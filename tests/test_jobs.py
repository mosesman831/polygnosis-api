"""Durable SQLite JobStore tests."""

from __future__ import annotations

from polygnosis_api.jobs import JobStore
from polygnosis_api.schemas import JobStatus


def _store(tmp_path) -> JobStore:
    return JobStore(str(tmp_path / "jobs.db"))


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


def test_fail_stale_running_on_init(tmp_path):
    db = str(tmp_path / "jobs.db")
    store1 = JobStore(db)
    job = store1.create({"objective": "a"})
    store1.claim_next("worker-1")
    assert store1.get(job.job_id).status == JobStatus.running

    # A new store (i.e. a process restart) reaps the orphaned running job.
    store2 = JobStore(db)
    reaped = store2.get(job.job_id)
    assert reaped is not None
    assert reaped.status == JobStatus.failed
    assert "restart" in (reaped.error or "").lower()


def test_fail_stale_running_returns_count(tmp_path):
    store = _store(tmp_path)
    a = store.create({"objective": "a"})
    b = store.create({"objective": "b"})
    store.update(a.job_id, status=JobStatus.running)
    store.update(b.job_id, status=JobStatus.running)
    assert store.fail_stale_running("Shutting down") == 2
    assert store.get(a.job_id).status == JobStatus.failed
    assert store.get(b.job_id).status == JobStatus.failed
