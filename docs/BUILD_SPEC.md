# Build spec — PolyGnosis API v0.2 (production / OSS cut)

Status: **ready to build**. This freezes choices from `docs/PRODUCTION_OSS_PLAN.md`.
Implement everything in this document. Do not invent extras outside Deferred.

Target version: **`0.2.0`** (honest pre-1.0). Bump `pyproject.toml` only; import version from package metadata.

---

## Product contract (frozen)

### Auth
- Env: `POLYGNOSIS_SERVICE_API_KEY` (inbound). Separate from `POLYGNOSIS_API_KEY` (outbound gateway).
- If `SERVICE_API_KEY` is **empty**: allow all requests but log a loud warning at startup (local-dev convenience).
- If set: require `Authorization: Bearer <key>` **or** `X-API-Key: <key>` on `/v1/*`. `/health` and `/ready` stay public.
- OpenAPI: HTTPBearer security scheme on boardroom routes.

### Concurrency
- `POLYGNOSIS_MAX_IN_FLIGHT` (default `2`). Count of `queued`+`running` jobs.
- Over limit → HTTP `429` with `{"detail":"Too many in-flight boardrooms"}`.

### Jobs (durable)
- SQLite at `POLYGNOSIS_JOBS_DB` (default `./data/jobs.db`).
- Replace in-memory `JobStore` entirely.
- Schema columns: `job_id TEXT PK`, `status`, `phase`, `detail`, `error`, `request_json`, `result_json`, `artifacts_path`, `created_at`, `updated_at`, `claimed_at`, `lease_owner`.
- API create: insert `queued`, return 202. Do **not** spawn daemon threads per request.
- **Worker**: one background thread started on FastAPI lifespan. Loop: claim next `queued` (or expired lease), set `running` + lease, execute pipeline, write result, clear lease. Poll interval ~0.5s.
- On process start: any `running` jobs with stale/missing lease → mark `failed` with error `Interrupted by process restart` (v0.2: no resume mid-pipeline).
- `get(job_id)` reads SQLite. Survives restart for completed/failed/queued.

### Job statuses
```
queued | running | completed | completed_degraded | failed
```
- `completed`: consensus ranking non-empty (or early-resolution path with synthetic ranking) and no critical phase failure.
- `completed_degraded`: finished with usable `final_output` but warnings (e.g. quality gate `ERROR`, partial dead solvers above quorum, gate unknown). Always include `warnings: string[]` on result.
- `failed`: below quorum, scoring produced empty ranking (non-early path), uncaught exception, interrupted restart.

### Result schema (HTTP)
`BoardroomResult` must include:
- existing fields **except** omit `artifacts_dir` from public response (keep on disk + DB column only)
- `scoring: dict | None` (Layer-1 / early-res note)
- `warnings: list[str]` (default `[]`)
- `degraded: bool` (true iff status would be `completed_degraded`)
- `phase_outcomes: list[{phase, status: ok|degraded|failed, detail}]`
- `consensus_ranking: dict[str, ConsensusEntry]` where `ConsensusEntry` has `rank: int`, optional `avg_rank`, `rrf_score`, `borda_score`, `score`, `note`

### Quality gate
- Non-JSON / empty → verdict `ERROR` (not PASS). Keep synthesis as `final_output`, add warning, mark degraded.
- Explicit `FAIL` → fallback to top individual (unchanged), verdict `FAIL`.
- Disabled → `quality_gate: null`, not degraded for that reason alone.

### Scoring failure (non-early)
- Empty/missing `rankings` after scorer → **fail the job** (`failed`), do not synthesize a fake winner.

### Early resolution
- Write `scoring.json` with `_early_resolution`, `_consensus_ranking`.
- Synthetic ranking entries: `{rank: 1, avg_rank: 1.0, rrf_score: 0.0, borda_score: 0.0, note: "early_resolution"}` for each alive solver (stable order).

### LLM client
- Sync `complete` only (remove unused `acomplete`).
- Retries: up to `POLYGNOSIS_LLM_MAX_RETRIES` (default `3`) on timeout, connect error, HTTP 429/500/502/503.
- Exponential backoff: 1s, 2s, 4s (+ small jitter).
- Return `""` only after retries exhausted (pipeline treats as empty).
- Log attempt number + status.

### Packaging
- Copy default `config.yaml` into package as `polygnosis_api/default_config.yaml` (also keep repo-root `config.yaml` as the editable checkout copy; sync content).
- Discovery order for config: `POLYGNOSIS_CONFIG_PATH` if set and exists → `./config.yaml` CWD → packaged `default_config.yaml`.
- Remove fragile `parents[2]` assumption.
- `requirements.txt`: runtime deps only (no pytest).
- Version `0.2.0` from `importlib.metadata.version("polygnosis-api")` with fallback constant in `__init__.py`.

### Settings (new env knobs)
```
POLYGNOSIS_SERVICE_API_KEY=
POLYGNOSIS_MAX_IN_FLIGHT=2
POLYGNOSIS_JOBS_DB=./data/jobs.db
POLYGNOSIS_LLM_MAX_RETRIES=3
POLYGNOSIS_REFLEXION_ENABLED=false
POLYGNOSIS_OBJECTIVE_MAX_CHARS=20000
```

### Reflexion
- Default **off** (`reflexion_enabled=false`). When off: empty injection, do not write buffer.
- When on: existing file buffer (single-operator local use). Document as not multi-tenant safe.

### Pipeline polish
- `build_orchestrator_prompt(objective, solver_count)` — ask for exactly `solver_count` personas.
- Add `models.scorer` in config (default same as synthesizer); `_scoring` uses `scorer` role.
- Remove unused YAML `settings.artifacts_dir` (env owns artifacts path).
- Progress detail uses `solver_count`, not `len(personas)`.

### Endpoints
| Method | Path | Auth | Notes |
|--------|------|------|-------|
| GET | `/health` | no | liveness `{status, version, protocol}` |
| GET | `/ready` | no | 200 if config loadable + (if service key mode) ok; 503 if config missing. Check outbound key present (warn-level in body if empty but still 200 for local?). **Decision: `/ready` returns 503 if boardroom config cannot load; 200 with `gateway_key_configured: bool`.** |
| POST | `/v1/boardroom` | yes if key set | 202; validate objective max chars |
| GET | `/v1/boardroom/{id}` | yes if key set | poll |

### Caps
- `objective` max length: `POLYGNOSIS_OBJECTIVE_MAX_CHARS` (default 20000) → 422 if over.

### Logging
- Include `job_id=` in worker/pipeline log lines when known.
- Startup log: version, max_in_flight, reflexion on/off, auth required yes/no.

### Graceful shutdown
- Lifespan shutdown: set flag so worker stops claiming; wait up to 10s for current job; if still running, leave row as `running` then on next start mark failed (stale lease). Simpler acceptable: on shutdown mark current in-flight `failed` with `Shutting down`.

### Consensus ties (small fix)
- When scores tie on an axis, break ties by `solution_id` ascending (deterministic). Document in code comment.
- Hybrid final rank: sort by `(avg_rank, solution_id)`.

### Tests (must pass)
1. Consensus: existing + ties + missing axis + rrf/borda shapes + custom k
2. Config discovery: packaged default loads
3. Auth: with key set → 401 without; 202 with
4. 429 when max in-flight saturated (insert running rows or mock)
5. Job persist: create, restart store instance, get still works
6. Pipeline with FakeLLM:
   - happy path hybrid ranking present
   - scoring empty → failed job/exception path
   - quality gate non-JSON → ERROR + degraded warnings
   - quality gate FAIL → top individual
   - early resolution path writes synthetic ranking fields
   - below quorum → raises/fails
7. LLM retry: mock httpx 500 then 200 → success

### CI
- `.github/workflows/ci.yml`: on push/PR → Python 3.11/3.12 → `pip install -e ".[dev]"` → `ruff check` → `pytest`

### Docs
- Update `README.md`, `SPEC.md`, `.env.example` to match this contract.
- Mark `docs/PRODUCTION_OSS_PLAN.md` status as **accepted → building in 0.2**.

---

## Deferred (do not build now)

- Hermes/Eve agent executors / Solver protocol
- Redis/Postgres multi-instance
- Job cancel / TTL cleanup endpoints
- Artifact HTTP download API
- Live gateway e2e in CI
- Resume mid-pipeline after crash
- CORS middleware (document server-to-server only)
- Changing GPL license

---

## File ownership for parallel implementers

| Owner | Files |
|-------|-------|
| A Packaging | `pyproject.toml`, `requirements.txt`, `src/polygnosis_api/__init__.py`, `src/polygnosis_api/config.py`, `src/polygnosis_api/default_config.yaml`, root `config.yaml` (scorer model, drop artifacts_dir), `.env.example` |
| B LLM + consensus | `src/polygnosis_api/llm.py`, `src/polygnosis_api/consensus.py`, `tests/test_consensus.py`, `tests/test_llm.py` |
| C Schemas + jobs | `src/polygnosis_api/schemas.py`, `src/polygnosis_api/jobs.py` |
| D Pipeline + prompts + reflexion | `src/polygnosis_api/pipeline.py`, `src/polygnosis_api/prompts.py`, `src/polygnosis_api/reflexion.py` |
| E API wiring | `src/polygnosis_api/main.py` |
| F Tests/CI/docs | `tests/test_api.py`, `tests/test_pipeline.py`, `tests/test_jobs.py`, `tests/test_config.py`, `.github/workflows/ci.yml`, `README.md`, `SPEC.md`, `docs/PRODUCTION_OSS_PLAN.md` |

Integrate in order A∥B∥C → D → E → F if conflicts; otherwise parallel with the interfaces below.

## Interface contracts (for parallel work)

### `LLMClient.complete(...) -> str`
Unchanged signature; retries internal.

### `JobStore`
```python
class JobStore:
    def __init__(self, db_path: str) -> None: ...
    def create(self, request: dict) -> Job: ...
    def get(self, job_id: str) -> Job | None: ...
    def update(self, job_id, *, status=None, phase=None, detail=None, error=None, result=None, artifacts_path=None) -> Job | None: ...
    def count_in_flight(self) -> int: ...  # queued + running
    def claim_next(self, worker_id: str, lease_seconds: int = 3600) -> Job | None: ...
    def fail_stale_running(self, reason: str) -> int: ...
```
`Job` dataclass adds `artifacts_path: str | None = None`.

### `BoardroomPipeline.run` return dict
Must include: `scoring`, `warnings`, `degraded`, `phase_outcomes`; must **not** require `artifacts_dir` in HTTP (main strips it). May still return `artifacts_dir` internally for DB.

### `ReflexionBuffer`
Add `enabled: bool = True` to `__init__`; when False, `load→[]`, `injection→""`, `ingest` no-op, `save` no-op.

### Auth dependency
`require_service_key` FastAPI dependency used by E.

---

## Acceptance checklist

- [ ] `pip install -e .` works; config loads without checkout hacks
- [ ] Version reports `0.2.0`
- [ ] Auth + 429 work
- [ ] Jobs survive process restart (SQLite)
- [ ] Degraded/failed honesty as specified
- [x] `scoring` present on completed responses (covered by tests)
- [x] CI green (`ruff` + `pytest`; `tests/` for config, jobs, api, pipeline)
- [x] README/SPEC match behavior
