"""API tests — isolated from the global store/worker via monkeypatch.

The app builds a module-level ``settings`` and ``store`` at import time and
starts a background worker on lifespan. Each test points those at a tmp SQLite
DB and stubs ``BoardroomPipeline.run`` so no real LLM calls happen.
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

import polygnosis_api.main as main_mod
from polygnosis_api.jobs import JobStore
from polygnosis_api.pipeline import BoardroomPipeline
from polygnosis_api.schemas import JobStatus


def _fake_result(
    objective: str,
    job_id: str | None,
    artifacts_dir: str,
    *,
    include_solutions: bool = True,
) -> dict:
    return {
        "job_id": job_id or "x",
        "objective": objective,
        "final_output": "ok",
        "scoring_algorithm": "hybrid",
        "consensus_ranking": {
            "s0": {"rank": 1, "avg_rank": 1.0, "rrf_score": 0.1, "borda_score": 2}
        },
        "scoring": {"rankings": []},
        "warnings": [],
        "degraded": False,
        "phase_outcomes": [],
        "trail": [
            {
                "solution_id": "s0",
                "solver": "Expert A",
                "rank": 1,
                # Mirror the real pipeline: full text only when requested. main
                # also strips it belt-and-braces when include_solutions is false.
                "solution": "FULL SOLUTION TEXT" if include_solutions else None,
            }
        ],
        "artifacts_dir": artifacts_dir,
        "reflexion_buffer_size": 0,
    }


def _client(tmp_path, monkeypatch, service_key: str = "") -> TestClient:
    db = str(tmp_path / "jobs.db")
    monkeypatch.setattr(main_mod.settings, "jobs_db", db)
    monkeypatch.setattr(main_mod.settings, "service_api_key", service_key)
    monkeypatch.setattr(main_mod.settings, "max_in_flight", 2)
    monkeypatch.setattr(main_mod.settings, "api_key", "test-gateway")
    monkeypatch.setattr(main_mod.settings, "reflexion_enabled", False)
    monkeypatch.setattr(main_mod.settings, "artifacts_dir", str(tmp_path / "artifacts"))
    monkeypatch.setattr(
        main_mod.settings, "corrections_buffer", str(tmp_path / "buf.json")
    )

    def fake_run(self, objective, *, job_id=None, on_progress=None, include_solutions=True):
        if on_progress:
            on_progress("complete", None)
        return _fake_result(
            objective,
            job_id,
            str(tmp_path / "artifacts" / "run"),
            include_solutions=include_solutions,
        )

    monkeypatch.setattr(BoardroomPipeline, "run", fake_run)
    monkeypatch.setattr(main_mod, "store", JobStore(db))
    return TestClient(main_mod.app)


def test_health(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["protocol"] == "polygnosis-v3"


def test_ready(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.get("/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["config_loaded"] is True
    assert body["gateway_key_configured"] is True
    assert body["auth_required"] is False


def test_auth_401_without_header(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, service_key="secret")
    r = client.post("/v1/boardroom", json={"objective": "design a thing"})
    assert r.status_code == 401


def test_auth_202_with_bearer(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, service_key="secret")
    r = client.post(
        "/v1/boardroom",
        json={"objective": "design a thing"},
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status_code == 202
    assert r.json()["status"] == "queued"


def test_auth_202_with_x_api_key(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, service_key="secret")
    r = client.post(
        "/v1/boardroom",
        json={"objective": "design a thing"},
        headers={"X-API-Key": "secret"},
    )
    assert r.status_code == 202


def test_429_when_saturated(tmp_path, monkeypatch):
    # No lifespan → no worker, so pre-inserted running rows stay in-flight.
    client = _client(tmp_path, monkeypatch)
    for _ in range(main_mod.settings.max_in_flight):
        job = main_mod.store.create({"objective": "busy"})
        main_mod.store.update(job.job_id, status=JobStatus.running)
    r = client.post("/v1/boardroom", json={"objective": "one more"})
    assert r.status_code == 429
    assert r.json()["detail"] == "Too many in-flight boardrooms"


def test_404_missing_job(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    r = client.get("/v1/boardroom/does-not-exist")
    assert r.status_code == 404


def test_objective_over_max_returns_422(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod.settings, "objective_max_chars", 50)
    client = _client(tmp_path, monkeypatch)
    r = client.post("/v1/boardroom", json={"objective": "y" * 51})
    assert r.status_code == 422


def test_ready_includes_jobs_counts(tmp_path, monkeypatch):
    # S5: /ready exposes a per-status jobs count dict via count_by_status().
    client = _client(tmp_path, monkeypatch)
    main_mod.store.update(
        main_mod.store.create({"objective": "seed"}).job_id, status=JobStatus.running
    )
    r = client.get("/ready")
    assert r.status_code == 200
    jobs = r.json()["jobs"]
    assert isinstance(jobs, dict)
    # Every status key is present (0 when none), and the seeded row is counted.
    for status in JobStatus:
        assert status.value in jobs
    assert jobs["running"] == 1


def _backdate(store: JobStore, job_id: str, created_at: str) -> None:
    """Force a job's created_at so list ordering is deterministic in tests."""
    store._conn.execute(
        "UPDATE jobs SET created_at = ? WHERE job_id = ?", (created_at, job_id)
    )
    store._conn.commit()


def test_list_boardroom_newest_first(tmp_path, monkeypatch):
    # N2: GET /v1/boardroom lists jobs newest-first and requires auth.
    client = _client(tmp_path, monkeypatch, service_key="secret")
    headers = {"Authorization": "Bearer secret"}

    ids = []
    for i in range(3):
        job = main_mod.store.create({"objective": f"obj{i}"})
        _backdate(main_mod.store, job.job_id, f"2020-01-01T00:00:0{i}Z")
        ids.append(job.job_id)

    r = client.get("/v1/boardroom", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3
    returned = [job["job_id"] for job in body["jobs"]]
    # Newest (largest created_at) first → reverse of insertion order.
    assert returned == list(reversed(ids))


def test_list_boardroom_requires_auth(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch, service_key="secret")
    r = client.get("/v1/boardroom")
    assert r.status_code == 401


def test_list_boardroom_respects_limit(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    for i in range(5):
        main_mod.store.create({"objective": f"obj{i}"})
    r = client.get("/v1/boardroom", params={"limit": 2})
    assert r.status_code == 200
    assert r.json()["count"] == 2


def _poll_until_terminal(client, job_id: str) -> dict:
    deadline = time.time() + 10
    while time.time() < deadline:
        poll = client.get(f"/v1/boardroom/{job_id}")
        assert poll.status_code == 200
        body = poll.json()
        if body["status"] in ("completed", "completed_degraded", "failed"):
            return body
        time.sleep(0.1)
    raise AssertionError("job did not reach a terminal status in time")


def test_include_solutions_false_nulls_trail(tmp_path, monkeypatch):
    # S9 (default): trail solution text is null in the HTTP body.
    with _client(tmp_path, monkeypatch) as client:
        r = client.post(
            "/v1/boardroom",
            json={"objective": "solve it", "include_solutions": False},
        )
        assert r.status_code == 202
        body = _poll_until_terminal(client, r.json()["job_id"])

    assert body["status"] == "completed"
    trail = body["result"]["trail"]
    assert trail
    assert all(item["solution"] is None for item in trail)


def test_include_solutions_true_keeps_trail(tmp_path, monkeypatch):
    # S9: opt-in returns the full solution text in the HTTP trail.
    with _client(tmp_path, monkeypatch) as client:
        r = client.post(
            "/v1/boardroom",
            json={"objective": "solve it", "include_solutions": True},
        )
        assert r.status_code == 202
        body = _poll_until_terminal(client, r.json()["job_id"])

    assert body["status"] == "completed"
    trail = body["result"]["trail"]
    assert trail
    assert trail[0]["solution"] == "FULL SOLUTION TEXT"


def test_post_then_poll_completed(tmp_path, monkeypatch):
    # The `with` block runs lifespan, which starts the background worker.
    with _client(tmp_path, monkeypatch) as client:
        r = client.post("/v1/boardroom", json={"objective": "solve it"})
        assert r.status_code == 202
        job_id = r.json()["job_id"]

        body = None
        deadline = time.time() + 10
        while time.time() < deadline:
            poll = client.get(f"/v1/boardroom/{job_id}")
            assert poll.status_code == 200
            body = poll.json()
            if body["status"] in ("completed", "completed_degraded", "failed"):
                break
            time.sleep(0.1)

    assert body is not None
    assert body["status"] == "completed"
    result = body["result"]
    assert result is not None
    # artifacts_dir is stripped from the public HTTP body.
    assert "artifacts_dir" not in result
    assert result["scoring"] is not None
    assert result["final_output"] == "ok"
    assert result["consensus_ranking"]["s0"]["rank"] == 1
