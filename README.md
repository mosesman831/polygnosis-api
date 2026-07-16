# PolyGnosis API

> **LatticeAG · Poly series** · Adversarial multi-model consensus as a public HTTP service
> **Version 0.3.0** · Protocol `polygnosis-v3`

Full [PolyGnosis](https://github.com/mosesman831/PolyGnosis) v3 boardroom over HTTP — orchestrate, parallel solve, early resolution, adversarial critique, **RRF + Borda** formal scoring, synthesis, constitutional quality gate, meta-review.

Self-contained package intended as its own repo (`mosesman831/polygnosis-api`). Solvers are **model completions** (OpenAI-compatible / AI Gateway), not Hermes agent sessions.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

There is no `requirements.txt` — dependencies live in `pyproject.toml`. Use `pip install -e .` for runtime only, or `pip install -e ".[dev]"` to also get `pytest` and `ruff`.

Set both keys in `.env`:

- `POLYGNOSIS_API_KEY` — outbound key for the LLM gateway (the boardroom calls it).
- `POLYGNOSIS_SERVICE_API_KEY` — inbound key clients must send. Leave empty for local dev and `/v1/*` is open (the server logs a loud warning at startup). See [SECURITY.md](SECURITY.md) — open mode is **dev-only**.

Run the server with the installed entrypoint or uvicorn:

```bash
polygnosis-api
# or
uvicorn polygnosis_api.main:app --host 0.0.0.0 --port 8080
```

```bash
# Start a boardroom (async — full pipeline takes minutes)
curl -X POST http://localhost:8080/v1/boardroom \
  -H 'content-type: application/json' \
  -H 'authorization: Bearer <POLYGNOSIS_SERVICE_API_KEY>' \
  -d '{"objective":"Design a production-grade JWT auth middleware in Rust"}'

# Poll
curl http://localhost:8080/v1/boardroom/<job_id> \
  -H 'authorization: Bearer <POLYGNOSIS_SERVICE_API_KEY>'

# List recent jobs (newest first)
curl 'http://localhost:8080/v1/boardroom?limit=20' \
  -H 'authorization: Bearer <POLYGNOSIS_SERVICE_API_KEY>'
```

The `Authorization` header (or `X-API-Key: <key>`) is required on every `/v1/*` route only when `POLYGNOSIS_SERVICE_API_KEY` is set. `/health` and `/ready` stay public. Auth comparisons use `hmac.compare_digest` (constant-time).

## Job lifecycle

`POST /v1/boardroom` inserts a `queued` job and returns `202`. A single background worker (started on lifespan) claims and runs it. Poll `GET /v1/boardroom/{job_id}` for status:

```
queued | running | completed | completed_degraded | failed
```

- `completed` — consensus ranking present, no critical phase failure.
- `completed_degraded` — usable `final_output` with `warnings` (e.g. quality gate returned non-JSON, dead solvers above quorum, or non-heterogeneous solver models).
- `failed` — below quorum, empty scoring on the non-early path, an uncaught error, shutdown mid-run, or a reclaimed stale lease.

### Durability, leases and WAL

Jobs are durable: they live in SQLite at `POLYGNOSIS_JOBS_DB` (default `./data/jobs.db`) and survive restarts. The store opens the DB in **WAL** mode (`journal_mode=WAL`, `busy_timeout=5000`, `synchronous=NORMAL`) for safe concurrent reads/writes.

The worker claims jobs with a **lease** (`POLYGNOSIS_JOB_LEASE_SECONDS`, default `3600`). `claim_next` takes the oldest `queued` job, or a `running` job whose lease has expired (missing or older than the lease window). The lease is renewed on every progress tick. On startup, only running rows with an **expired** lease are failed — a peer's fresh lease is never stolen. The default deployment is single-process (one API + one worker); leases make restart recovery accurate rather than enabling multi-instance fan-out. v0.3 has **no mid-pipeline resume**: a reclaimed job restarts from scratch (prior `phase`/`result`/`error` are cleared).

## Workflow

```
POST /v1/boardroom
  ↓
0  Orchestrate → problem statement + dynamic personas
1  Parallel solve (heterogeneous models, one per solver slot)
1.5 Early resolution (skip critique+scoring if unanimous)
2  Adversarial critique + Reflexion buffer (+ optional revise rounds)
3  LLM per-axis scores → deterministic RRF / Borda / hybrid ranking
4  Meta-synthesis
5  Constitutional quality gate
6  Meta-review
  ↓
GET /v1/boardroom/{job_id} → final_output + consensus_ranking + trail
```

### Debate rounds

`max_debate_rounds = N` means **N critique passes**; revisions happen *between* rounds. So `N=1` is critique only (no revise); `N=2` is critique → revise → critique; and so on. Configure it in `config.yaml` or per request (`"max_debate_rounds": N`).

### Heterogeneity

Distinct solver models make the boardroom adversarial. If two solver slots resolve to the same model id (e.g. both fell back to the default), the solve phase is marked `degraded` and a `"Solver models not fully heterogeneous: ..."` warning is attached — the run still completes.

## Consensus scoring

Layer 1: LLM scores each solution on correctness, efficiency, maintainability, robustness, security (0–10).

Layer 2 (deterministic):

| Algorithm | Formula |
|-----------|---------|
| **RRF** | `Σ 1/(k + rank_axis)` — default `k=60` |
| **Borda** | `Σ (n - 1 - rank_axis)` |
| **Hybrid** (default) | average of RRF and Borda rank positions |

Request override: `"scoring_algorithm": "rrf" | "borda" | "hybrid"`. For `rrf`/`borda` the matching named field (`rrf_score`/`borda_score`) is mirrored from the generic score so the trail/API are never mysteriously null.

## API surface

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | no | Liveness: `{status, version, protocol}` — `protocol` is the `PROTOCOL_VERSION` constant (`polygnosis-v3`) |
| `GET` | `/ready` | no | Readiness: `200` with `config_loaded`, `gateway_key_configured`, `auth_required`, `jobs` (per-status counts); `503` if config cannot load |
| `POST` | `/v1/boardroom` | if key set | Start consensus job (`202`); objective over `POLYGNOSIS_OBJECTIVE_MAX_CHARS` → `422`; over `POLYGNOSIS_MAX_IN_FLIGHT` → `429` (atomic capacity check) |
| `GET` | `/v1/boardroom` | if key set | List recent jobs, newest first (`limit` default `20`, max `100`) |
| `GET` | `/v1/boardroom/{job_id}` | if key set | Poll status / result (`404` if unknown) |

The public result omits `artifacts_dir` — run artifacts stay on disk (path stored on the job row only).

### Request fields

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `objective` | string | — | Required; capped by `POLYGNOSIS_OBJECTIVE_MAX_CHARS` (422 over limit) |
| `scoring_algorithm` | `rrf`\|`borda`\|`hybrid` | config | Override the ranking algorithm |
| `solver_count` | int 2–5 | config | Number of solver slots |
| `early_resolution` | bool | config | Skip critique+scoring on unanimous quorum |
| `quality_gate` | bool | config | Enable the constitutional quality gate |
| `max_debate_rounds` | int 1–5 | config | Critique passes (see above) |
| `include_solutions` | bool | `false` | When `false`, `trail[].solution` is `null` in the HTTP body (full text always stays in artifact files); `true` returns the full solver text |

## Config

- `config.yaml` — per-role models (including `scorer`), timeouts, scoring algorithm, debate rounds, and per-role sampling (`temperature_*`, `max_tokens_*`). Discovery order: `POLYGNOSIS_CONFIG_PATH`, then `./config.yaml`, then the packaged `default_config.yaml` (so `pip install` works without a checkout). Loaded once at startup and cached; a restart picks up edits.
- `.env` — see `.env.example` for all knobs.

### Per-role sampling

Solvers run hotter for diversity; the scorer is deterministic. Both `temperature` and `max_tokens` are sent to the gateway when set. Defaults apply to any role not listed:

| Setting | Default |
|---------|---------|
| `temperature_solver` | `0.5` |
| `temperature_critic` | `0.2` |
| `temperature_scorer` | `0.0` |
| `temperature_default` | `0.3` |
| `max_tokens_solver` | `8192` |
| `max_tokens_default` | `4096` |

Works with any OpenAI-compatible chat completions endpoint (Vercel AI Gateway, OpenAI, OpenRouter, LexGateway, etc.). `LLMClient` owns one pooled `httpx.Client` (closed on shutdown) with a global concurrency cap (`POLYGNOSIS_MAX_LLM_CONCURRENCY`, default `8`).

## Observability

- Request logging middleware: method, path, status, `duration_ms` (bodies are never logged).
- Per-phase wall-clock timings written to `timings.json` in each run directory (`{phase: duration_sec}`), flushed as the run progresses.
- `/ready` reports live job counts per status.

## Reflexion

Cross-run correction buffer, **off by default** (`POLYGNOSIS_REFLEXION_ENABLED=false`). It is a single-operator local feature and is not multi-tenant safe — leave it off for shared deployments.

## Security

`/v1/*` is open when `POLYGNOSIS_SERVICE_API_KEY` is empty — this **dev-only** mode logs a loud warning at startup. Set the key for any public bind. See [SECURITY.md](SECURITY.md).

## Contributing

Dev setup, linting, and test conventions live in [CONTRIBUTING.md](CONTRIBUTING.md). In short: `pip install -e ".[dev]"`, then `ruff check src tests` and `pytest`.

## Relationship to PolyGnosis (Hermes skill)

| | Hermes PolyGnosis | PolyGnosis API |
|--|-------------------|----------------|
| Runtime | `hermes chat` agent sessions + tools | Model completions only |
| Interface | Skill / CLI pipeline | HTTP JSON |
| Consensus | RRF + Borda hybrid | Same algorithms |
| Latency | 10–20+ minutes | Same order (async jobs) |

Agent-backed hosted boardrooms (Eve / Hermes) are a future tier — this repo ships the full protocol on models first.

## License

GPL-3.0 — same as [PolyGnosis](https://github.com/mosesman831/PolyGnosis).
