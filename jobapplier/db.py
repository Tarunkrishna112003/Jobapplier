"""SQLite tracker: one row per job, carrying its application state."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .models import Job, STATUS_QUEUED

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    key           TEXT PRIMARY KEY,
    company       TEXT NOT NULL,
    title         TEXT NOT NULL,
    url           TEXT NOT NULL,
    location      TEXT,
    ats           TEXT,
    job_id        TEXT,
    department    TEXT,
    posted_at     TEXT,
    remote        INTEGER DEFAULT 0,
    score         REAL DEFAULT 0,
    reasons       TEXT,
    status        TEXT NOT NULL DEFAULT 'queued',
    status_note   TEXT,
    filled_fields TEXT,
    screenshot    TEXT,
    discovered_at TEXT,
    updated_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_score  ON jobs(score DESC);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    job_key   TEXT NOT NULL,
    ts        TEXT NOT NULL,
    kind      TEXT NOT NULL,
    detail    TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_key);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Tracker:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def upsert_jobs(self, jobs: Iterable[Job]) -> tuple[int, int]:
        """Insert new jobs; refresh score/metadata on ones already tracked.

        An existing row's status is never overwritten - a job you already
        submitted stays submitted across re-runs of discovery.
        """
        new = updated = 0
        for j in jobs:
            row = self.conn.execute("SELECT key FROM jobs WHERE key = ?", (j.key,)).fetchone()
            if row:
                self.conn.execute(
                    "UPDATE jobs SET score=?, reasons=?, location=?, posted_at=?, url=?, updated_at=? WHERE key=?",
                    (j.score, json.dumps(j.reasons), j.location, j.posted_at, j.url, _now(), j.key),
                )
                updated += 1
            else:
                self.conn.execute(
                    """INSERT INTO jobs (key, company, title, url, location, ats, job_id, department,
                                         posted_at, remote, score, reasons, status, discovered_at, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (j.key, j.company, j.title, j.url, j.location, j.ats, j.job_id, j.department,
                     j.posted_at, int(j.remote), j.score, json.dumps(j.reasons), STATUS_QUEUED, _now(), _now()),
                )
                new += 1
        self.conn.commit()
        return new, updated

    def set_status(self, key: str, status: str, note: str = "", **extra: Any) -> None:
        sets = ["status = ?", "status_note = ?", "updated_at = ?"]
        vals: list[Any] = [status, note, _now()]
        for col in ("filled_fields", "screenshot"):
            if col in extra:
                sets.append(f"{col} = ?")
                v = extra[col]
                vals.append(json.dumps(v) if not isinstance(v, str) else v)
        vals.append(key)
        self.conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE key = ?", vals)
        self.log(key, status, note)
        self.conn.commit()

    def log(self, key: str, kind: str, detail: str = "") -> None:
        self.conn.execute(
            "INSERT INTO events (job_key, ts, kind, detail) VALUES (?,?,?,?)",
            (key, _now(), kind, detail),
        )
        self.conn.commit()

    def queue(self, limit: int = 25, status: str = STATUS_QUEUED,
              company: str | None = None, min_score: float = 0.0) -> list[sqlite3.Row]:
        sql = "SELECT * FROM jobs WHERE status = ? AND score >= ?"
        params: list[Any] = [status, min_score]
        if company:
            sql += " AND company LIKE ?"
            params.append(f"%{company}%")
        sql += " ORDER BY score DESC LIMIT ?"
        params.append(limit)
        return self.conn.execute(sql, params).fetchall()

    def get(self, key: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM jobs WHERE key = ?", (key,)).fetchone()

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) c FROM jobs GROUP BY status").fetchall()
        return {r["status"]: r["c"] for r in rows}

    def all_jobs(self, order: str = "score DESC") -> list[sqlite3.Row]:
        return self.conn.execute(f"SELECT * FROM jobs ORDER BY {order}").fetchall()
