"""Transient enrollment. All durable writes go through Store."""
import hashlib
import secrets
import sqlite3
from dataclasses import dataclass, field
from threading import RLock
from time import monotonic

import numpy as np
from fastapi import HTTPException

from vision_models import MODEL_VERSION, cosine, normalize

SAMPLE_COUNT = 5
SESSION_TTL = 300
MAX_SESSIONS = 32


@dataclass
class Enrollment:
    name: str
    email: str
    public_id: str | None
    expires: float
    samples: list = field(default_factory=list)
    fingerprints: set = field(default_factory=set)
    last_sample: float = float("-inf")


class Enrollments:
    def __init__(self, store, engine, clock=monotonic):
        self.store = store
        self.engine = engine
        self.clock = clock
        self.lock = RLock()
        self.sessions = {}

    def start(self, name, email, public_id):
        if not self.engine.ready:
            raise HTTPException(503, "Face models are unavailable.")
        if public_id:
            existing = self.store.profile(public_id)
            if not existing:
                raise HTTPException(404, "Profile not found.")
            name, email = existing["full_name"], existing["email"]
        elif self.store.email_exists(email):
            raise HTTPException(409, "A profile already uses this email.")
        with self.lock:
            now = self.clock()
            self.sessions = {key: session for key, session in self.sessions.items() if session.expires > now}
            if len(self.sessions) >= MAX_SESSIONS:
                raise HTTPException(429, "Too many active enrollment sessions.")
            key = secrets.token_urlsafe(32)
            self.sessions[key] = Enrollment(name, email, public_id, now + SESSION_TTL)
        return {"session_id": key, "required": SAMPLE_COUNT, "expires_in": SESSION_TTL}

    def get(self, key):
        session = self.sessions.get(key)
        if session is None or self.clock() >= session.expires:
            self.sessions.pop(key, None)
            raise HTTPException(410, "Enrollment expired. Start again.")
        return session

    def sample(self, key, frame):
        fingerprint = hashlib.sha256(frame).digest()
        with self.lock:
            session = self.get(key)
            if len(session.samples) >= SAMPLE_COUNT:
                return {"quality": "complete", "count": SAMPLE_COUNT, "required": SAMPLE_COUNT}
            if fingerprint in session.fingerprints or self.clock() - session.last_sample < 0.45:
                return {"quality": "change_pose", "count": len(session.samples), "required": SAMPLE_COUNT}
        analysis = self.engine.analyze(frame)
        with self.lock:
            session = self.get(key)
            quality = analysis.quality
            if len(session.samples) >= SAMPLE_COUNT:
                quality = "complete"
            elif fingerprint in session.fingerprints or self.clock() - session.last_sample < 0.45:
                quality = "change_pose"
            elif analysis.embedding is not None:
                if session.samples and cosine(session.samples[0], analysis.embedding) < 0.363:
                    quality = "different_face"
                else:
                    session.samples.append(analysis.embedding)
                    session.fingerprints.add(fingerprint)
                    session.last_sample = self.clock()
            return {
                "quality": quality, "count": len(session.samples), "required": SAMPLE_COUNT,
                "box": analysis.box, "detection_confidence": analysis.detection_confidence,
            }

    def complete(self, key):
        with self.lock:
            session = self.get(key)
            if len(session.samples) != SAMPLE_COUNT:
                raise HTTPException(409, "Collect five usable face samples first.")
            vector = normalize(np.mean(session.samples, axis=0))
            try:
                profile = self.store.save_enrollment(
                    session.name, session.email, vector.tobytes(), SAMPLE_COUNT, MODEL_VERSION, session.public_id
                )
            except sqlite3.IntegrityError as exc:
                raise HTTPException(409, "A profile already uses this email.") from exc
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            self.sessions.pop(key)
            return profile

    def cancel(self, key):
        with self.lock:
            self.sessions.pop(key, None)

    def prune(self):
        with self.lock:
            now = self.clock()
            for key, session in list(self.sessions.items()):
                if now >= session.expires:
                    self.sessions.pop(key)

