# PolyGnosis API

> **LatticeAG · Poly series** · Adversarial multi-model consensus as a public HTTP service

Full [PolyGnosis](https://github.com/mosesman831/PolyGnosis) v3 boardroom over HTTP — orchestrate, parallel solve, early resolution, adversarial critique, **RRF + Borda** formal scoring, synthesis, constitutional quality gate, meta-review.

Self-contained package intended as its own repo (`mosesman831/polygnosis-api`). Solvers are **model completions** (OpenAI-compatible / AI Gateway), not Hermes agent sessions.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Set both keys in `.env`:

- `POLYGNOSIS_API_KEY` — outbound key for the LLM gateway (the boardroom calls it).
- `POLYGNOSIS_SERVICE_API_KEY` — inbound key clients must send. Leave empty for local dev and `/v1/*` is open (the server logs a loud warning at startup).

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
```

The `Authorization` header (or `X-API-Key: <key>`) is required only when `POLYGNOSIS_SERVICE_API_KEY` is set. `/health` and `/ready` stay public.

## Job lifecycle

`POST /v1/boardroom` inserts a `queued` job and returns `202`. A single background worker (started on lifespan) claims and runs it. Poll `GET /v1/boardroom/{job_id}` for status:

```
queued | running | completed | completed_degraded | failed
```

- `completed` — consensus ranking present, no critical phase failure.
- `completed_degraded` — usable `final_output` with `warnings` (e.g. quality gate returned non-JSON, or dead solvers above quorum).
- `failed` — below quorum, empty scoring on the non-early path, an uncaught error, or interruption by a process restart.

Jobs are durable: they live in SQLite at `POLYGNOSIS_JOBS_DB` (default `./data/jobs.db`) and survive restarts. On startup, any job left `running` is marked `failed` (v0.2 does not resume mid-pipeline).

## Workflow

```
POST /v1/boardroom
  ↓
0  Orchestrate → problem statement + dynamic personas
1  Parallel solve (3+ heterogeneous models)
1.5 Early resolution (skip critique+scoring if unanimous)
2  Adversarial critique + Reflexion buffer (+ optional revise rounds)
3  LLM per-axis scores → deterministic RRF / Borda / hybrid ranking
4  Meta-synthesis
5  Constitutional quality gate
6  Meta-review
  ↓
GET /v1/boardroom/{job_id} → final_output + consensus_ranking + trail
```

## Consensus scoring

Layer 1: LLM scores each solution on correctness, efficiency, maintainability, robustness, security (0–10).

Layer 2 (deterministic):

| Algorithm | Formula |
|-----------|---------|
| **RRF** | `Σ 1/(k + rank_axis)` — default `k=60` |
| **Borda** | `Σ (n - 1 - rank_axis)` |
| **Hybrid** (default) | average of RRF and Borda rank positions |

Request override: `"scoring_algorithm": "rrf" | "borda" | "hybrid"`.

## API surface

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | no | Liveness: `{status, version, protocol}` |
| `GET` | `/ready` | no | Readiness: `200` with `config_loaded`, `gateway_key_configured`, `auth_required`; `503` if config cannot load |
| `POST` | `/v1/boardroom` | if key set | Start consensus job (`202`); objective over `POLYGNOSIS_OBJECTIVE_MAX_CHARS` → `422`; over `POLYGNOSIS_MAX_IN_FLIGHT` → `429` |
| `GET` | `/v1/boardroom/{job_id}` | if key set | Poll status / result (`404` if unknown) |

The public result omits `artifacts_dir` — run artifacts stay on disk (path stored on the job row only).

## Config

- `config.yaml` — per-role models (including `scorer`), timeouts, scoring algorithm, debate rounds. Discovery order: `POLYGNOSIS_CONFIG_PATH`, then `./config.yaml`, then the packaged `default_config.yaml` (so `pip install` works without a checkout).
- `.env` — see `.env.example` for all knobs.

Works with any OpenAI-compatible chat completions endpoint (Vercel AI Gateway, OpenAI, OpenRouter, LexGateway, etc.).

## Reflexion

Cross-run correction buffer, **off by default** (`POLYGNOSIS_REFLEXION_ENABLED=false`). It is a single-operator local feature and is not multi-tenant safe — leave it off for shared deployments.

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
