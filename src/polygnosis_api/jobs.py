"""Durable SQLite-backed job store for long-running boardroom runs."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from polygnosis_api.schemas import JobStatus

_IN_FLIGHT = (JobStatus.queued.value, JobStatus.running.value)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class Job:
    job_id: str
    status: JobStatus
    created_at: str
    updated_at: str
    phase: str | None = None
    detail: str | None = None
    error: str | None = None
    request: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    artifacts_path: str | None = None


class JobStore:
    """Persistent job store backed by stdlib sqlite3.

    A single connection is shared across threads (check_same_thread=False)
    and guarded by a threading.Lock so all operations are serialized.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create_table()
        # On process start, any job still marked running belongs to a worker
        # that no longer exists (v0.2 has no mid-pipeline resume).
        self.fail_stale_running("Interrupted by process restart")

    def _create_table(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    phase TEXT,
                    detail TEXT,
                    error TEXT,
                    request_json TEXT,
                    result_json TEXT,
                    artifacts_path TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    claimed_at TEXT,
                    lease_owner TEXT
                )
                """
            )
            self._conn.commit()

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job(
            job_id=row["job_id"],
            status=JobStatus(row["status"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            phase=row["phase"],
            detail=row["detail"],
            error=row["error"],
            request=json.loads(row["request_json"]) if row["request_json"] else {},
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            artifacts_path=row["artifacts_path"],
        )

    def create(self, request: dict[str, Any]) -> Job:
        now = _now()
        job = Job(
            job_id=str(uuid.uuid4()),
            status=JobStatus.queued,
            created_at=now,
            updated_at=now,
            request=request,
        )
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO jobs (
                    job_id, status, phase, detail, error,
                    request_json, result_json, artifacts_path,
                    created_at, updated_at, claimed_at, lease_owner
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.status.value,
                    None,
                    None,
                    None,
                    json.dumps(request),
                    None,
                    None,
                    now,
                    now,
                    None,
                    None,
                ),
            )
            self._conn.commit()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        if row is None:
            return None
        # Build a fresh Job from row data so callers never mutate shared state.
        return self._row_to_job(row)

    def update(
        self,
        job_id: str,
        *,
        status: JobStatus | str | None = None,
        phase: str | None = None,
        detail: str | None = None,
        error: str | None = None,
        result: dict[str, Any] | None = None,
        artifacts_path: str | None = None,
    ) -> Job | None:
        sets: list[str] = []
        params: list[Any] = []
        if status is not None:
            status_value = status.value if isinstance(status, JobStatus) else str(status)
            sets.append("status = ?")
            params.append(status_value)
        if phase is not None:
            sets.append("phase = ?")
            params.append(phase)
        if detail is not None:
            sets.append("detail = ?")
            params.append(detail)
        if error is not None:
            sets.append("error = ?")
            params.append(error)
        if result is not None:
            sets.append("result_json = ?")
            params.append(json.dumps(result))
        if artifacts_path is not None:
            sets.append("artifacts_path = ?")
            params.append(artifacts_path)

        sets.append("updated_at = ?")
        params.append(_now())

        with self._lock:
            exists = self._conn.execute(
                "SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if exists is None:
                return None
            params.append(job_id)
            self._conn.execute(
                f"UPDATE jobs SET {', '.join(sets)} WHERE job_id = ?", params
            )
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._row_to_job(row)

    def count_in_flight(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE status IN (?, ?)",
                _IN_FLIGHT,
            ).fetchone()
        return int(row["n"])

    def claim_next(self, worker_id: str, lease_seconds: int = 3600) -> Job | None:
        """Atomically claim the oldest queued job for a worker.

        Selecting and flipping to running happen under the same lock so two
        workers cannot claim the same row.
        """
        now = _now()
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM jobs
                WHERE status = ?
                ORDER BY created_at ASC, job_id ASC
                LIMIT 1
                """,
                (JobStatus.queued.value,),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                """
                UPDATE jobs
                SET status = ?, claimed_at = ?, lease_owner = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (
                    JobStatus.running.value,
                    now,
                    worker_id,
                    now,
                    row["job_id"],
                ),
            )
            self._conn.commit()
            claimed = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)
            ).fetchone()
        return self._row_to_job(claimed)

    def fail_stale_running(self, reason: str) -> int:
        """Mark all currently-running jobs as failed. Returns count affected."""
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE jobs
                SET status = ?, error = ?, lease_owner = NULL, updated_at = ?
                WHERE status = ?
                """,
                (
                    JobStatus.failed.value,
                    reason,
                    now,
                    JobStatus.running.value,
                ),
            )
            self._conn.commit()
            return cur.rowcount
