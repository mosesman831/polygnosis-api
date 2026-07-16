"""PolyGnosis API — FastAPI entrypoint.

Owner E (API wiring). Responsibilities:
- One durable SQLite-backed JobStore + a single background worker thread.
- Auth, concurrency (429), objective caps (422), health/ready.
- Map the pipeline result onto job status (completed / completed_degraded / failed).

The API only *creates* queued jobs; the background worker claims and runs them.
No per-request daemon threads.
"""

from __future__ import annotations

import contextlib
import copy
import logging
import os
import threading
from collections.abc import AsyncIterator
from pathlib import Path

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from polygnosis_api import __version__
from polygnosis_api.config import Settings, load_boardroom_config
from polygnosis_api.jobs import Job, JobStore
from polygnosis_api.llm import LLMClient
from polygnosis_api.pipeline import BoardroomPipeline
from polygnosis_api.reflexion import ReflexionBuffer
from polygnosis_api.schemas import (
    BoardroomCreateResponse,
    BoardroomJobResponse,
    BoardroomRequest,
    BoardroomResult,
    HealthResponse,
    JobStatus,
    ReadyResponse,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("polygnosis_api")

settings = Settings()
store = JobStore(settings.jobs_db)

# ── worker plumbing ──────────────────────────────────────────────────────────
_worker_id = f"worker-{os.getpid()}"
_worker_stop = threading.Event()
_worker_thread: threading.Thread | None = None
_current_lock = threading.Lock()
_current_job_id: str | None = None
_IDLE_SLEEP_SEC = 0.5
_SHUTDOWN_JOIN_SEC = 10.0


def _build_reflexion() -> ReflexionBuffer:
    """Construct the reflexion buffer, honouring `reflexion_enabled` when the
    (Owner-D) constructor supports it. Falls back to the single-arg form so this
    module keeps working before that change lands."""
    try:
        return ReflexionBuffer(settings.corrections_buffer, enabled=settings.reflexion_enabled)
    except TypeError:
        return ReflexionBuffer(settings.corrections_buffer)


def _build_config(request: BoardroomRequest) -> dict:
    """Config overlay: same logic as the pre-worker `_run_boardroom`."""
    cfg = copy.deepcopy(load_boardroom_config(settings.config_path))
    settings_block = cfg.setdefault("settings", {})
    if request.scoring_algorithm is not None:
        settings_block["scoring_algorithm"] = request.scoring_algorithm.value
    if request.solver_count is not None:
        settings_block["solver_count"] = request.solver_count
    if request.early_resolution is not None:
        settings_block["early_resolution_enabled"] = request.early_resolution
    if request.quality_gate is not None:
        settings_block["quality_gate_enabled"] = request.quality_gate
    if request.max_debate_rounds is not None:
        settings_block["max_debate_rounds"] = request.max_debate_rounds
    return cfg


def _run_job(job: Job) -> None:
    job_id = job.job_id
    try:
        request = BoardroomRequest(**job.request)
    except Exception as exc:  # noqa: BLE001 — malformed persisted request
        logger.error("job_id=%s invalid stored request: %s", job_id, exc)
        store.update(
            job_id,
            status=JobStatus.failed,
            phase="failed",
            error=f"Invalid request: {exc}",
        )
        return

    try:
        cfg = _build_config(request)

        artifacts = Path(settings.artifacts_dir)
        artifacts.mkdir(parents=True, exist_ok=True)

        pipeline = BoardroomPipeline(
            cfg=cfg,
            llm=LLMClient(settings),
            reflexion=_build_reflexion(),
            artifacts_root=artifacts,
        )

        def on_progress(phase: str, detail: str | None) -> None:
            store.update(job_id, phase=phase, detail=detail)

        result = pipeline.run(request.objective, job_id=job_id, on_progress=on_progress)

        # artifacts_dir lives on disk + the DB column only; never in the HTTP body.
        artifacts_path = result.pop("artifacts_dir", None)

        # Forward-compatible with Owner D: these keys may be absent until the
        # pipeline starts emitting degraded/warnings/phase_outcomes.
        degraded = bool(result.get("degraded", False))
        _ = result.get("warnings", [])  # ensure present downstream if pipeline omits it
        status = JobStatus.completed_degraded if degraded else JobStatus.completed

        store.update(
            job_id,
            status=status,
            phase="complete",
            detail=None,
            result=result,
            artifacts_path=artifacts_path,
        )
        logger.info("job_id=%s finished status=%s", job_id, status.value)
    except Exception as exc:  # noqa: BLE001 — any pipeline failure → failed job
        logger.exception("job_id=%s boardroom failed", job_id)
        store.update(job_id, status=JobStatus.failed, phase="failed", error=str(exc))


def _worker_loop() -> None:
    global _current_job_id
    logger.info("worker %s started", _worker_id)
    while not _worker_stop.is_set():
        try:
            job = store.claim_next(_worker_id)
        except Exception:  # noqa: BLE001 — never let the loop die on a store hiccup
            logger.exception("worker %s failed to claim next job", _worker_id)
            _worker_stop.wait(_IDLE_SLEEP_SEC)
            continue

        if job is None:
            _worker_stop.wait(_IDLE_SLEEP_SEC)
            continue

        with _current_lock:
            _current_job_id = job.job_id
        try:
            _run_job(job)
        finally:
            with _current_lock:
                _current_job_id = None
    logger.info("worker %s stopped", _worker_id)


@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    global _worker_thread
    logger.info(
        "PolyGnosis API v%s starting — max_in_flight=%s reflexion=%s auth_required=%s",
        __version__,
        settings.max_in_flight,
        "on" if settings.reflexion_enabled else "off",
        "yes" if settings.service_api_key else "no",
    )
    if not settings.service_api_key:
        logger.warning(
            "POLYGNOSIS_SERVICE_API_KEY is empty — /v1 routes are OPEN (local-dev mode)"
        )

    _worker_stop.clear()
    _worker_thread = threading.Thread(
        target=_worker_loop, name="boardroom-worker", daemon=True
    )
    _worker_thread.start()
    try:
        yield
    finally:
        _worker_stop.set()
        if _worker_thread is not None:
            _worker_thread.join(timeout=_SHUTDOWN_JOIN_SEC)
        # If a job is still running after the grace period, mark it failed so it
        # isn't left dangling (BUILD_SPEC: "mark current in-flight failed with
        # 'Shutting down'"). A stale row would otherwise be reaped on next start.
        with _current_lock:
            pending = _current_job_id
        if pending:
            current = store.get(pending)
            if current is not None and current.status == JobStatus.running:
                store.update(
                    pending,
                    status=JobStatus.failed,
                    phase="failed",
                    error="Shutting down",
                )


app = FastAPI(
    title="PolyGnosis API",
    description=(
        "Adversarial multi-model consensus protocol as a public HTTP API. "
        "Full PolyGnosis v3 workflow: orchestrate → parallel solve → early resolution → "
        "critique → RRF+Borda scoring → synthesis → quality gate → meta-review."
    ),
    version=__version__,
    lifespan=lifespan,
)

# auto_error=False so missing/other credentials fall through to the X-API-Key
# check and open-mode logic; declaring it still advertises Bearer in OpenAPI.
_bearer_scheme = HTTPBearer(auto_error=False)


def require_service_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """Gate `/v1/*`: open when no service key configured, else require a matching
    `Authorization: Bearer <key>` or `X-API-Key: <key>`."""
    expected = settings.service_api_key
    if not expected:
        return
    if (
        credentials is not None
        and credentials.scheme.lower() == "bearer"
        and credentials.credentials == expected
    ):
        return
    if x_api_key is not None and x_api_key == expected:
        return
    raise HTTPException(status_code=401, detail="Invalid or missing service API key")


def _job_response(job: Job) -> BoardroomJobResponse:
    result = None
    if job.result:
        data = dict(job.result)
        # Defensive: strip artifacts_dir if an older result row still carries it.
        data.pop("artifacts_dir", None)
        # Pydantic coerces plain consensus_ranking dict values into ConsensusEntry.
        result = BoardroomResult(**data)
    return BoardroomJobResponse(
        job_id=job.job_id,
        status=job.status,
        phase=job.phase,
        detail=job.detail,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
        result=result,
    )


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(status="ok", version=__version__)


@app.get("/ready", response_model=ReadyResponse)
def ready() -> ReadyResponse:
    try:
        load_boardroom_config(settings.config_path)
    except Exception as exc:  # noqa: BLE001 — any load failure → not ready
        raise HTTPException(
            status_code=503, detail=f"Boardroom config not loadable: {exc}"
        ) from exc
    return ReadyResponse(
        status="ok",
        version=__version__,
        config_loaded=True,
        gateway_key_configured=bool(settings.api_key),
        auth_required=bool(settings.service_api_key),
    )


@app.post(
    "/v1/boardroom",
    response_model=BoardroomCreateResponse,
    status_code=202,
    dependencies=[Depends(require_service_key)],
)
def create_boardroom(body: BoardroomRequest) -> BoardroomCreateResponse:
    if len(body.objective) > settings.objective_max_chars:
        raise HTTPException(
            status_code=422,
            detail=(
                f"objective exceeds max length of {settings.objective_max_chars} "
                "characters"
            ),
        )
    if store.count_in_flight() >= settings.max_in_flight:
        raise HTTPException(status_code=429, detail="Too many in-flight boardrooms")

    job = store.create(body.model_dump(mode="json"))
    return BoardroomCreateResponse(
        job_id=job.job_id,
        status=JobStatus.queued,
        poll_url=f"/v1/boardroom/{job.job_id}",
    )


@app.get(
    "/v1/boardroom/{job_id}",
    response_model=BoardroomJobResponse,
    dependencies=[Depends(require_service_key)],
)
def get_boardroom(job_id: str) -> BoardroomJobResponse:
    job = store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_response(job)


def cli() -> None:
    uvicorn.run(
        "polygnosis_api.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    cli()
