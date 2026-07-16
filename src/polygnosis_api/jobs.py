"""Durable SQLite-backed job store for long-running boardroom runs."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from polygnosis_api.schemas import JobStatus

_IN_FLIGHT = (JobStatus.queued.value, JobStatus.running.value)

# Default lease window. A running job whose lease is older than this (or that
# never recorded a claim) is considered abandoned and may be reclaimed/failed.
_DEFAULT_LEASE_SECONDS = 3600

_TS_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _now() -> str:
    return time.strftime(_TS_FORMAT, time.gmtime())


def _parse_ts(value: str) -> datetime:
    """Parse a timestamp stored by :func:`_now` into a UTC-aware datetime.

    Accepts the canonical ``_now`` format as well as any ISO-8601 string with a
    trailing ``Z``. Naive results are assumed to be UTC.
    """
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1]
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _lease_expired(
    claimed_at: str | None,
    lease_seconds: int,
    now_dt: datetime | None = None,
) -> bool:
    """True if a running row's lease is stale (or was never claimed)."""
    if claimed_at is None:
        return True
    now_dt = now_dt or datetime.now(UTC)
    try:
        claimed_dt = _parse_ts(claimed_at)
    except (ValueError, TypeError):
        # Unparseable timestamps are treated as expired so the row is recoverable.
        return True
    return (now_dt - claimed_dt).total_seconds() >= lease_seconds


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

    The default deployment is single-process: one API process with one worker.
    Real leases (``claimed_at`` / ``lease_owner``) make restart recovery
    accurate — on restart only expired leases are reclaimed/failed, so a
    peer's fresh lease is never stolen.
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._configure_connection()
        self._create_table()
        # On process start, reclaim jobs whose lease has expired. Fresh leases
        # (e.g. owned by a still-running peer) are left alone.
        self.fail_stale_running("Interrupted by process restart")

    def _configure_connection(self) -> None:
        # WAL improves read/write concurrency; busy_timeout avoids immediate
        # "database is locked" errors; NORMAL synchronous is safe under WAL.
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.commit()

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

    def _insert_job(self, job: Job) -> None:
        """Insert a queued job row. Caller must hold ``self._lock``."""
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
                job.phase,
                job.detail,
                job.error,
                json.dumps(job.request),
                None,
                job.artifacts_path,
                job.created_at,
                job.updated_at,
                None,
                None,
            ),
        )
        self._conn.commit()

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
            self._insert_job(job)
        return job

    def create_if_capacity(
        self, request: dict[str, Any], max_in_flight: int
    ) -> Job | None:
        """Atomically create a job only if in-flight capacity remains.

        Counting queued+running and the insert happen under a single lock so
        concurrent callers cannot both slip past a full queue. Returns the new
        ``Job`` on success or ``None`` when at/over ``max_in_flight``.
        """
        now = _now()
        job = Job(
            job_id=str(uuid.uuid4()),
            status=JobStatus.queued,
            created_at=now,
            updated_at=now,
            request=request,
        )
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE status IN (?, ?)",
                _IN_FLIGHT,
            ).fetchone()
            if int(row["n"]) >= max_in_flight:
                return None
            self._insert_job(job)
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

    def count_by_status(self) -> dict[str, int]:
        """Return a count per :class:`JobStatus` value (0 when none present)."""
        counts: dict[str, int] = {status.value: 0 for status in JobStatus}
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
            ).fetchall()
        for row in rows:
            counts[row["status"]] = int(row["n"])
        return counts

    def claim_next(
        self, worker_id: str, lease_seconds: int = _DEFAULT_LEASE_SECONDS
    ) -> Job | None:
        """Atomically claim the oldest claimable job for a worker.

        A row is claimable when it is ``queued`` or when it is ``running`` with
        an expired lease (``claimed_at`` missing or older than ``lease_seconds``).
        Selecting and flipping to running happen under the same lock so two
        workers cannot claim the same row.

        Reclaimed running jobs restart from scratch (v0.3 has no mid-pipeline
        resume): ``phase``/``detail``/``error``/``result`` are cleared and a
        fresh ``claimed_at``/``lease_owner`` recorded.
        """
        now = _now()
        now_dt = _parse_ts(now)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM jobs
                WHERE status IN (?, ?)
                ORDER BY created_at ASC, job_id ASC
                """,
                _IN_FLIGHT,
            ).fetchall()
            target = None
            for row in rows:
                if row["status"] == JobStatus.queued.value:
                    target = row
                    break
                # running: claimable only when its lease has expired.
                if _lease_expired(row["claimed_at"], lease_seconds, now_dt):
                    target = row
                    break
            if target is None:
                return None
            self._conn.execute(
                """
                UPDATE jobs
                SET status = ?, phase = NULL, detail = NULL, error = NULL,
                    result_json = NULL, claimed_at = ?, lease_owner = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (
                    JobStatus.running.value,
                    now,
                    worker_id,
                    now,
                    target["job_id"],
                ),
            )
            self._conn.commit()
            claimed = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (target["job_id"],)
            ).fetchone()
        return self._row_to_job(claimed)

    def renew_lease(self, job_id: str, worker_id: str) -> bool:
        """Refresh ``claimed_at`` for a job owned by ``worker_id``.

        Returns True when a matching leased row was updated, False otherwise
        (unknown job or the lease belongs to a different worker).
        """
        now = _now()
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE jobs
                SET claimed_at = ?, updated_at = ?
                WHERE job_id = ? AND lease_owner = ?
                """,
                (now, now, job_id, worker_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def fail_stale_running(
        self, reason: str, lease_seconds: int = _DEFAULT_LEASE_SECONDS
    ) -> int:
        """Fail running jobs whose lease has expired. Returns count affected.

        Only rows with an expired lease (or a missing ``claimed_at``) are
        failed; peer-fresh leases are left untouched. Called on ``__init__`` so
        restart recovery reaps abandoned work without stealing live leases.
        """
        now = _now()
        now_dt = _parse_ts(now)
        with self._lock:
            rows = self._conn.execute(
                "SELECT job_id, claimed_at FROM jobs WHERE status = ?",
                (JobStatus.running.value,),
            ).fetchall()
            stale = [
                row["job_id"]
                for row in rows
                if _lease_expired(row["claimed_at"], lease_seconds, now_dt)
            ]
            for job_id in stale:
                self._conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?, error = ?, lease_owner = NULL, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (JobStatus.failed.value, reason, now, job_id),
                )
            self._conn.commit()
        return len(stale)
