"""SQLite persistence for enrolled profiles and attendance."""
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
DEFAULT_DB = ROOT / "data" / "visionattend.db"


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Store:
    def __init__(self, path=DEFAULT_DB):
        self.path = Path(os.environ.get("VISIONATTEND_DB", path))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS users (
                    uuid TEXT PRIMARY KEY,
                    public_id TEXT NOT NULL UNIQUE,
                    full_name TEXT NOT NULL,
                    email TEXT NOT NULL UNIQUE,
                    consent_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS face_templates (
                    user_uuid TEXT PRIMARY KEY REFERENCES users(uuid) ON DELETE CASCADE,
                    embedding BLOB NOT NULL,
                    model_version TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    sample_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS attendance (
                    id INTEGER PRIMARY KEY,
                    user_uuid TEXT NOT NULL REFERENCES users(uuid) ON DELETE CASCADE,
                    attendance_date TEXT NOT NULL,
                    checked_in_at TEXT NOT NULL,
                    similarity REAL NOT NULL,
                    UNIQUE (user_uuid, attendance_date)
                );
                CREATE INDEX IF NOT EXISTS attendance_date_idx ON attendance(attendance_date);
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    threshold REAL NOT NULL,
                    stable_frames INTEGER NOT NULL,
                    last_public_id INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO settings VALUES (1, 0.363, 3, 0);
            """)

    def profile(self, public_id):
        with self.connect() as db:
            row = db.execute("""
                SELECT u.*, t.sample_count FROM users u JOIN face_templates t ON t.user_uuid=u.uuid
                WHERE u.public_id=?
            """, (public_id,)).fetchone()
        return public_profile(row) if row else None

    def email_exists(self, email):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone() is not None

    def save_enrollment(self, name, email, vector, sample_count, model_version, public_id=None):
        now = utc_now()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if public_id:
                row = db.execute("SELECT uuid FROM users WHERE public_id=?", (public_id,)).fetchone()
                if row is None:
                    raise LookupError("Profile no longer exists.")
                uid = row["uuid"]
                db.execute("UPDATE users SET consent_at=? WHERE uuid=?", (now, uid))
            else:
                uid = str(uuid4())
                number = db.execute(
                    "UPDATE settings SET last_public_id=last_public_id+1 WHERE id=1 RETURNING last_public_id"
                ).fetchone()[0]
                public_id = f"VA-{number:06d}"
                db.execute("INSERT INTO users VALUES (?, ?, ?, ?, ?, ?)", (uid, public_id, name, email, now, now))
            db.execute("""
                INSERT INTO face_templates VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_uuid) DO UPDATE SET embedding=excluded.embedding,
                    model_version=excluded.model_version, revision=excluded.revision,
                    sample_count=excluded.sample_count, created_at=excluded.created_at
            """, (uid, vector, model_version, str(uuid4()), sample_count, now))
            profile = db.execute("""
                SELECT u.*, t.sample_count FROM users u JOIN face_templates t ON t.user_uuid=u.uuid
                WHERE u.uuid=?
            """, (uid,)).fetchone()
            return public_profile(profile)

    def template(self, public_id):
        with self.connect() as db:
            return db.execute("""
                SELECT u.uuid, u.public_id, u.full_name, t.embedding, t.model_version, t.revision
                FROM users u JOIN face_templates t ON t.user_uuid=u.uuid WHERE u.public_id=?
            """, (public_id,)).fetchone()

    def recognition_data(self, model_version):
        with self.connect() as db:
            db.execute("BEGIN")
            policy = dict(db.execute("SELECT threshold, stable_frames FROM settings WHERE id=1").fetchone())
            templates = db.execute("""
                SELECT u.uuid, u.public_id, u.full_name, t.embedding, t.revision
                FROM face_templates t JOIN users u ON t.user_uuid=u.uuid WHERE t.model_version=?
            """, (model_version,)).fetchall()
        return policy, templates

    def users(self, search, page, size):
        pattern = like_pattern(search)
        clause = "WHERE (u.full_name LIKE ? ESCAPE '!' OR u.email LIKE ? ESCAPE '!' OR u.public_id LIKE ? ESCAPE '!')"
        params = (pattern,) * 3
        with self.connect() as db:
            db.execute("BEGIN")
            total = db.execute(f"SELECT count(*) FROM users u {clause}", params).fetchone()[0]
            rows = db.execute(f"""
                SELECT u.*, t.sample_count FROM users u JOIN face_templates t ON t.user_uuid=u.uuid
                {clause} ORDER BY u.full_name COLLATE NOCASE, u.public_id LIMIT ? OFFSET ?
            """, (*params, size, (page - 1) * size)).fetchall()
        return {"items": [public_profile(row) for row in rows], "total": total}

    def delete(self, public_id):
        with self.connect() as db:
            return bool(db.execute("DELETE FROM users WHERE public_id=?", (public_id,)).rowcount)

    def policy(self, threshold=None, stable_frames=None):
        with self.connect() as db:
            if threshold is not None:
                db.execute("UPDATE settings SET threshold=?, stable_frames=? WHERE id=1", (threshold, stable_frames))
            return dict(db.execute("SELECT threshold, stable_frames FROM settings WHERE id=1").fetchone())

    def mark(self, uid, revision, day, similarity, instant):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            user = db.execute("""
                SELECT u.public_id, u.full_name FROM users u JOIN face_templates t ON t.user_uuid=u.uuid
                WHERE u.uuid=? AND t.revision=?
            """, (uid, revision)).fetchone()
            if user is None:
                raise LookupError("The profile changed. Verify again.")
            created = db.execute("""
                INSERT INTO attendance (user_uuid, attendance_date, checked_in_at, similarity)
                VALUES (?, ?, ?, ?) ON CONFLICT(user_uuid, attendance_date) DO NOTHING
            """, (uid, day, instant.astimezone(timezone.utc).isoformat(timespec="milliseconds"), similarity)).rowcount
            row = db.execute("""
                SELECT attendance_date, checked_in_at, similarity FROM attendance
                WHERE user_uuid=? AND attendance_date=?
            """, (uid, day)).fetchone()
            return {**dict(user), **dict(row), "created": bool(created)}

    def records(self, start, end, public_id, search, page=1, size=None):
        checks = []
        params = []
        for value, clause in (
            (start, "a.attendance_date >= ?"),
            (end, "a.attendance_date <= ?"),
            (public_id, "u.public_id = ?"),
        ):
            if value:
                checks.append(clause)
                params.append(value)
        if search:
            checks.append("(u.full_name LIKE ? ESCAPE '!' OR u.email LIKE ? ESCAPE '!' OR u.public_id LIKE ? ESCAPE '!')")
            params.extend([like_pattern(search)] * 3)
        where = " WHERE " + " AND ".join(checks) if checks else ""
        source = "FROM attendance a JOIN users u ON a.user_uuid=u.uuid" + where
        with self.connect() as db:
            db.execute("BEGIN")
            total = db.execute("SELECT count(*) " + source, params).fetchone()[0]
            query = """
                SELECT a.attendance_date, a.checked_in_at, a.similarity, u.public_id, u.full_name
            """ + source + " ORDER BY a.checked_in_at DESC, a.id DESC"
            if size is not None:
                query += " LIMIT ? OFFSET ?"
                params.extend([size, (page - 1) * size])
            return {"items": [dict(row) for row in db.execute(query, params)], "total": total}

    def dashboard(self, today: date):
        days = [(today - timedelta(days=offset)).isoformat() for offset in range(6, -1, -1)]
        with self.connect() as db:
            db.execute("BEGIN")
            registered = db.execute("SELECT count(*) FROM users").fetchone()[0]
            counts = dict(db.execute("""
                SELECT attendance_date, count(*) FROM attendance
                WHERE attendance_date BETWEEN ? AND ? GROUP BY attendance_date
            """, (days[0], days[-1])).fetchall())
            recent = db.execute("""
                SELECT u.public_id, u.full_name, a.attendance_date, a.checked_in_at, a.similarity
                FROM attendance a JOIN users u ON u.uuid=a.user_uuid
                ORDER BY a.checked_in_at DESC, a.id DESC LIMIT 5
            """).fetchall()
        present = counts.get(days[-1], 0)
        return {
            "registered": registered, "present": present, "pending": registered - present,
            "rate": round(present / registered * 100, 1) if registered else 0,
            "days": [{"date": day, "count": counts.get(day, 0)} for day in days],
            "recent": [dict(row) for row in recent],
        }


def like_pattern(value):
    return "%" + value.strip().replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%"


def public_profile(row):
    return {key: row[key] for key in ("public_id", "full_name", "email", "created_at", "sample_count")}
