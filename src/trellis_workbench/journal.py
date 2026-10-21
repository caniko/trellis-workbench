"""SQLite record of jobs: what we asked for and what happened. Not a scheduler;
ComfyUI owns the queue."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from .schema import JobState, now

DDL = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    asset_id TEXT NOT NULL,
    revision INTEGER,
    backend TEXT NOT NULL,
    client_id TEXT NOT NULL,
    prompt_id TEXT NOT NULL UNIQUE,
    request_sha256 TEXT NOT NULL,
    state TEXT NOT NULL,
    request TEXT NOT NULL,      -- JSON: everything needed to inspect or intentionally rerun
    detail TEXT NOT NULL DEFAULT '',
    created TEXT NOT NULL,
    updated TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_events (
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class Job:
    job_id: str
    asset_id: str
    revision: int | None
    backend: str
    client_id: str
    prompt_id: str
    request_sha256: str
    state: JobState
    request: dict
    detail: str


class Journal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with closing(self._conn()) as c, c:
            c.executescript(DDL)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, isolation_level="IMMEDIATE")
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        return c

    def create(self, job: Job) -> None:
        ts = now().isoformat()
        with closing(self._conn()) as c, c:
            c.execute(
                "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    job.job_id,
                    job.asset_id,
                    job.revision,
                    job.backend,
                    job.client_id,
                    job.prompt_id,
                    job.request_sha256,
                    job.state.value,
                    json.dumps(job.request, sort_keys=True),
                    job.detail,
                    ts,
                    ts,
                ),
            )
            c.execute("INSERT INTO job_events VALUES (?,?,?,?)", (job.job_id, ts, "state", job.state.value))

    def set_state(self, job_id: str, state: JobState, detail: str = "") -> None:
        ts = now().isoformat()
        with closing(self._conn()) as c, c:
            n = c.execute(
                "UPDATE jobs SET state=?, detail=?, updated=? WHERE job_id=?", (state.value, detail, ts, job_id)
            ).rowcount
            if n != 1:
                raise KeyError(job_id)
            c.execute(
                "INSERT INTO job_events VALUES (?,?,?,?)", (job_id, ts, "state", f"{state.value} {detail}".strip())
            )

    def event(self, job_id: str, kind: str, detail: str) -> None:
        with closing(self._conn()) as c, c:
            c.execute("INSERT INTO job_events VALUES (?,?,?,?)", (job_id, now().isoformat(), kind, detail))

    def set_request(self, job_id: str, request: dict) -> None:
        with closing(self._conn()) as c, c:
            c.execute("UPDATE jobs SET request=? WHERE job_id=?", (json.dumps(request, sort_keys=True), job_id))

    def set_revision(self, job_id: str, revision: int) -> None:
        with closing(self._conn()) as c, c:
            c.execute("UPDATE jobs SET revision=? WHERE job_id=?", (revision, job_id))

    def get(self, job_id: str) -> Job:
        with closing(self._conn()) as c:
            row = c.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return _job(row)

    def find_request(self, request_sha256: str) -> list[Job]:
        """Earlier jobs for an identical request: reconcile those before submitting again."""
        with closing(self._conn()) as c:
            rows = c.execute("SELECT * FROM jobs WHERE request_sha256=? ORDER BY created", (request_sha256,)).fetchall()
        return [_job(r) for r in rows]

    def list(self, limit: int = 50) -> list[Job]:
        with closing(self._conn()) as c:
            rows = c.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
        return [_job(r) for r in rows]

    def events(self, job_id: str) -> list[tuple[str, str, str]]:
        with closing(self._conn()) as c:
            return c.execute(
                "SELECT at, kind, detail FROM job_events WHERE job_id=? ORDER BY rowid", (job_id,)
            ).fetchall()


def _job(row) -> Job:
    return Job(
        job_id=row[0],
        asset_id=row[1],
        revision=row[2],
        backend=row[3],
        client_id=row[4],
        prompt_id=row[5],
        request_sha256=row[6],
        state=JobState(row[7]),
        request=json.loads(row[8]),
        detail=row[9],
    )
