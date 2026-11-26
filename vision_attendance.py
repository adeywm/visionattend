"""Match faces in a live frame and record stable identities once per day."""
from collections import Counter
from datetime import datetime, timezone
from time import monotonic

import numpy as np

from vision_models import MODEL_VERSION, normalize

MATCH_GAP = 0.05
MAX_FRAME_GAP = 2.0


class LiveAttendance:
    def __init__(self, store, engine, zone, source_live=lambda: True,
                 clock=monotonic, wall_clock=lambda: datetime.now(timezone.utc)):
        self.store = store
        self.engine = engine
        self.zone = zone
        self.source_live = source_live
        self.clock = clock
        self.wall_clock = wall_clock
        self.streaks = {}
        self.seen = {}
        self.day = None
        self.policy = None
        self.last_frame = float("-inf")

    def process(self, frame):
        analysis = self.engine.analyze_all(frame)
        settings, rows = self.store.recognition_data(MODEL_VERSION)
        policy = (settings["threshold"], settings["stable_frames"])
        instant = self.wall_clock()
        day = instant.astimezone(self.zone).date().isoformat()
        now = self.clock()
        if self.day != day:
            self.day = day
            self.seen.clear()
        if self.policy != policy or now - self.last_frame > MAX_FRAME_GAP:
            self.streaks.clear()
        self.policy, self.last_frame = policy, now

        result = {
            "quality": analysis.quality, "latency_ms": analysis.latency_ms, **settings,
            "faces": [], "records": [],
        }
        if not self.source_live():
            self.streaks.clear()
            return {**result, "quality": "source_disconnected"}
        if not analysis.faces:
            self.streaks.clear()
            return result

        templates = []
        vectors = []
        for row in rows:
            try:
                vectors.append(normalize(np.frombuffer(row["embedding"], dtype=np.float32)))
            except ValueError:
                continue
            templates.append(row)
        matrix = np.stack(vectors) if vectors else None
        candidates = []
        for face in analysis.faces:
            item = {
                "box": face.box, "detection_confidence": face.detection_confidence,
                "similarity": None, "state": face.quality,
            }
            index = None
            if face.embedding is not None:
                item["state"] = "unknown"
                if matrix is not None:
                    scores = np.clip(matrix @ face.embedding, -1, 1)
                    best = int(np.argmax(scores))
                    score = float(scores[best])
                    item["similarity"] = score
                    if score >= policy[0]:
                        scores[best] = -1
                        if score - float(scores.max()) >= MATCH_GAP:
                            index = best
                        else:
                            item["state"] = "ambiguous"
            candidates.append((item, index))

        counts = Counter(index for _, index in candidates if index is not None)
        next_streaks = {}
        for item, index in candidates:
            result["faces"].append(item)
            if index is None:
                continue
            if counts[index] > 1:
                item["state"] = "ambiguous"
                continue
            row = templates[index]
            uid, revision = row["uuid"], row["revision"]
            key = (uid, revision)
            count = min(policy[1], self.streaks.get(key, 0) + 1)
            next_streaks[key] = count
            item.update(
                full_name=row["full_name"], public_id=row["public_id"],
                stable_count=count, state="matching",
            )
            saved = self.seen.get(uid)
            if saved is None and count == policy[1] and self.source_live():
                try:
                    saved = self.store.mark(uid, revision, day, item["similarity"], instant)
                except LookupError:
                    continue
                self.seen[uid] = saved
                result["records"].append(saved)
            if saved:
                item["state"] = "checked_in" if saved["created"] else "already_checked_in"
                item["checked_in_at"] = saved["checked_in_at"]
        self.streaks = next_streaks
        return result
