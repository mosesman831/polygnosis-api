"""Durable SQLite JobStore tests."""

from __future__ import annotations

from polygnosis_api.jobs import JobStore
from polygnosis_api.schemas import JobStatus


def _store(tmp_path) -> JobStore:
    return JobStore(str(tmp_path / "jobs.db"))


def _age_claimed_at(store: JobStore, job_id: str, iso: str = "2000-01-01T00:00:00Z") -> None:
    """Force a job's lease to look expired (test-only DB poke)."""
    with store._lock:
        store._conn.execute(
            "UPDATE jobs SET claimed_at = ? WHERE job_id = ?", (iso, job_id)
        )
        store._conn.commit()


def test_create_and_get(tmp_path):
    store = _store(tmp_path)
    job = store.create({"objective": "solve x"})
    assert job.status == JobStatus.queued
    fetched = store.get(job.job_id)
    assert fetched is not None
    assert fetched.job_id == job.job_id
    assert fetched.request["objective"] == "solve x"


def test_get_missing_returns_none(tmp_path):
    store = _store(tmp_path)
    assert store.get("does-not-exist") is None


def test_count_in_flight(tmp_path):
    store = _store(tmp_path)
    assert store.count_in_flight() == 0
    store.create({"objective": "a"})
    store.create({"objective": "b"})
    assert store.count_in_flight() == 2
    # completed jobs no longer count against in-flight.
    job = store.create({"objective": "c"})
    store.update(job.job_id, status=JobStatus.completed)
    assert store.count_in_flight() == 2


def test_claim_next_marks_running(tmp_path):
    store = _store(tmp_path)
    job = store.create({"objective": "a"})
    claimed = store.claim_next("worker-1")
    assert claimed is not None
    assert claimed.job_id == job.job_id
    assert claimed.status == JobStatus.running
    # A queue with nothing left to claim returns None.
    assert store.claim_next("worker-1") is None


def test_update_fields(tmp_path):
    store = _store(tmp_path)
    job = store.create({"objective": "a"})
    updated = store.update(
        job.job_id,
        status=JobStatus.completed,
        phase="complete",
        detail="done",
        result={"final_output": "ok"},
        artifacts_path="/tmp/art/x",
    )
    assert updated is not None
    assert updated.status == JobStatus.completed
    assert updated.phase == "complete"
    assert updated.detail == "done"
    assert updated.result == {"final_output": "ok"}
    assert updated.artifacts_path == "/tmp/art/x"


def test_update_missing_returns_none(tmp_path):
    store = _store(tmp_path)
    assert store.update("nope", status=JobStatus.failed) is None


def test_persist_across_instances(tmp_path):
    db = str(tmp_path / "jobs.db")
    store1 = JobStore(db)
    job = store1.create({"objective": "persist me"})
    # A fresh store instance on the same path still sees the job.
    store2 = JobStore(db)
    fetched = store2.get(job.job_id)
    assert fetched is not None
    assert fetched.request["objective"] == "persist me"


def test_fail_stale_running_on_init_reaps_expired(tmp_path):
    db = str(tmp_path / "jobs.db")
    store1 = JobStore(db)
    job = store1.create({"objective": "a"})
    store1.claim_next("worker-1")
    assert store1.get(job.job_id).status == JobStatus.running
    # Age the lease so it looks abandoned, then simulate a process restart.
    _age_claimed_at(store1, job.job_id)

    store2 = JobStore(db)
    reaped = store2.get(job.job_id)
    assert reaped is not None
    assert reaped.status == JobStatus.failed
    assert "restart" in (reaped.error or "").lower()


def test_fail_stale_running_on_init_keeps_fresh_lease(tmp_path):
    db = str(tmp_path / "jobs.db")
    store1 = JobStore(db)
    job = store1.create({"objective": "a"})
    store1.claim_next("worker-1")

    # Restart while the lease is still fresh: the running job is left alone
    # (a peer worker may still own it).
    store2 = JobStore(db)
    kept = store2.get(job.job_id)
    assert kept is not None
    assert kept.status == JobStatus.running


def test_fail_stale_running_returns_count(tmp_path):
    store = _store(tmp_path)
    a = store.create({"objective": "a"})
    b = store.create({"objective": "b"})
    # No claimed_at → treated as expired leases.
    store.update(a.job_id, status=JobStatus.running)
    store.update(b.job_id, status=JobStatus.running)
    assert store.fail_stale_running("Shutting down") == 2
    assert store.get(a.job_id).status == JobStatus.failed
    assert store.get(b.job_id).status == JobStatus.failed


def test_fail_stale_running_only_expired(tmp_path):
    store = _store(tmp_path)
    fresh = store.create({"objective": "fresh"})
    stale = store.create({"objective": "stale"})
    store.claim_next("worker-1")  # claims oldest (fresh) → fresh lease
    store.claim_next("worker-1")  # claims stale → fresh lease
    _age_claimed_at(store, stale.job_id)

    # Only the expired lease is failed; the fresh one keeps running.
    assert store.fail_stale_running("Shutting down") == 1
    assert store.get(stale.job_id).status == JobStatus.failed
    assert store.get(fresh.job_id).status == JobStatus.running


def test_create_if_capacity_returns_job_under_cap(tmp_path):
    store = _store(tmp_path)
    job = store.create_if_capacity({"objective": "a"}, max_in_flight=2)
    assert job is not None
    assert job.status == JobStatus.queued
    assert store.get(job.job_id) is not None


def test_create_if_capacity_none_when_full(tmp_path):
    store = _store(tmp_path)
    a = store.create_if_capacity({"objective": "a"}, max_in_flight=2)
    b = store.create_if_capacity({"objective": "b"}, max_in_flight=2)
    assert a is not None
    assert b is not None
    # At capacity → None, and no extra row is inserted.
    assert store.create_if_capacity({"objective": "c"}, max_in_flight=2) is None
    assert store.count_in_flight() == 2
    # Freeing a slot lets a new job through.
    store.update(a.job_id, status=JobStatus.completed)
    c = store.create_if_capacity({"objective": "c"}, max_in_flight=2)
    assert c is not None
    assert store.count_in_flight() == 2


def test_claim_next_reclaims_expired_running(tmp_path):
    store = _store(tmp_path)
    job = store.create({"objective": "a"})
    first = store.claim_next("worker-1", lease_seconds=3600)
    assert first is not None
    assert first.status == JobStatus.running
    # Record progress that must be discarded when the job restarts.
    store.update(job.job_id, phase="solve", detail="mid", error="oops", result={"x": 1})

    # Fresh lease → not reclaimable by a peer.
    assert store.claim_next("worker-2", lease_seconds=3600) is None

    _age_claimed_at(store, job.job_id)
    reclaimed = store.claim_next("worker-2", lease_seconds=3600)
    assert reclaimed is not None
    assert reclaimed.job_id == job.job_id
    assert reclaimed.status == JobStatus.running
    # Restart from scratch: prior phase/detail/error/result cleared.
    assert reclaimed.phase is None
    assert reclaimed.detail is None
    assert reclaimed.error is None
    assert reclaimed.result is None


def test_renew_lease(tmp_path):
    store = _store(tmp_path)
    job = store.create({"objective": "a"})
    store.claim_next("worker-1")

    # Owner renews; non-owner and unknown job cannot.
    assert store.renew_lease(job.job_id, "worker-1") is True
    assert store.renew_lease(job.job_id, "worker-2") is False
    assert store.renew_lease("does-not-exist", "worker-1") is False


def test_renew_lease_refreshes_expiry(tmp_path):
    store = _store(tmp_path)
    job = store.create({"objective": "a"})
    store.claim_next("worker-1")
    _age_claimed_at(store, job.job_id)

    # Renewing makes the lease fresh again → no longer reclaimable.
    assert store.renew_lease(job.job_id, "worker-1") is True
    assert store.claim_next("worker-2", lease_seconds=3600) is None


def test_count_by_status(tmp_path):
    store = _store(tmp_path)
    counts = store.count_by_status()
    assert set(counts.keys()) == {s.value for s in JobStatus}
    assert all(v == 0 for v in counts.values())

    a = store.create({"objective": "a"})
    store.create({"objective": "b"})
    c = store.create({"objective": "c"})
    store.update(a.job_id, status=JobStatus.running)
    store.update(c.job_id, status=JobStatus.completed_degraded)

    counts = store.count_by_status()
    assert counts[JobStatus.queued.value] == 1
    assert counts[JobStatus.running.value] == 1
    assert counts[JobStatus.completed_degraded.value] == 1
    assert counts[JobStatus.completed.value] == 0
    assert counts[JobStatus.failed.value] == 0
