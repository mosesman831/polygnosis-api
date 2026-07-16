# Build spec — PolyGnosis API v0.3 (improve every element)

Status: **ready to build** on top of v0.2.
Target version: **`0.3.0`**.

Do everything below. Do not invent Hermes/Eve, Redis multi-instance, or mid-pipeline resume.

---

## Must (correctness / trust)

### M1 — Real job leases
- `claim_next(worker_id, lease_seconds)` reclaims: oldest `queued`, **or** `running` whose `claimed_at` is older than `lease_seconds`.
- Reclaimed running jobs restart from scratch (v0.3 still no mid-pipeline resume); clear prior `result`/`error`/`phase` appropriately when reclaiming.
- `fail_stale_running` on init: only fail `running` rows with expired lease (or missing `claimed_at`), not peer-fresh leases. Document single-process default; leases make restart recovery accurate.
- Worker renews lease periodically during long runs (update `claimed_at` every ~60s or on each progress callback).

### M2 — Constant-time auth
- `hmac.compare_digest` for Bearer and `X-API-Key` comparisons in `require_service_key`.

### M3 — Atomic in-flight create
- `JobStore.create_if_capacity(request, max_in_flight) -> Job | None`
- Single locked transaction: count queued+running; if `>= max` return None; else insert.
- `POST /v1/boardroom` uses this; None → 429.

### M4 — Objective length authority
- Remove hard `max_length=20000` from `BoardroomRequest.objective` (keep `min_length=1`).
- Enforce only via `settings.objective_max_chars` in the route (422).

### M5 — Shutdown / terminal status race
- Worker checks stop flag before writing terminal `completed`/`completed_degraded`.
- If stop requested mid-run: mark `failed` with `Shutting down` and exit (do not flip to completed afterward).
- Lifespan still signals stop + join; avoid double-writers.

---

## Should (operability / fidelity)

### S1 — Pooled HTTP client
- `LLMClient` owns one `httpx.Client`; create in `__init__`, `close()` method; main lifespan closes on shutdown.
- Per-call timeout still applied (httpx supports timeout on request).

### S2 — Per-role temperature + max_tokens
- Config `settings` (and defaults):
  - `temperature_solver: 0.5`
  - `temperature_critic: 0.2`
  - `temperature_scorer: 0.0`
  - `temperature_default: 0.3`
  - `max_tokens_solver: 8192`
  - `max_tokens_default: 4096`
- `LLMClient.complete(..., temperature=..., max_tokens=...)` sends both in payload when set.
- Pipeline picks temps/max_tokens by phase/role.

### S3 — Heterogeneity warnings
- In `_parallel_solve`, if multiple solvers resolve to the same model id (or fallback), append warning: `"Solver models not fully heterogeneous: ..."`.
- Mark solve phase `degraded` when duplicates exist (still run).

### S4 — SQLite WAL + busy_timeout
- On connect: `PRAGMA journal_mode=WAL;` `PRAGMA busy_timeout=5000;` `PRAGMA synchronous=NORMAL;`

### S5 — Observability
- Request logging middleware: method, path, status, duration_ms (skip body).
- Phase timings: write `timings.json` in run dir `{phase: duration_sec}`.
- `/ready` adds `jobs: {queued, running, completed, completed_degraded, failed}` counts (new `JobStore.count_by_status()`).

### S6 — Ruff ruleset
```toml
[tool.ruff.lint]
select = ["E", "F", "I", "B", "UP"]
ignore = ["E501"]
```
Fix any new findings in owned files.

### S7 — Deps / dist hygiene
- Delete root `requirements.txt` **or** replace with a one-liner pointing to `pip install -e .` — prefer delete + README-only `pip install -e ".[dev]"`.
- Remove stale `dist/*1.0.0*` artifacts from repo (add `dist/` to `.gitignore` if not present).

### S8 — Cache boardroom config
- Load YAML once at startup into `main` module cache; `_build_config` deep-copies the cache then overlays request. Invalidate only on process restart (good enough).

### S9 — Trail payload control
- Request field `include_solutions: bool = False` (default false).
- When false: `trail[].solution = null` in HTTP result (full text remains in artifact files).
- When true: include full solutions (current behavior).

### S10 — Debate-round docs
- Comment in `config.yaml` + README: `max_debate_rounds=N` means N critique passes; revisions happen between rounds (so N=1 → critique only, no revise).

### S11 — Ranking shape honesty
- For `rrf`/`borda` modes, still attach sibling scores when cheap: keep `{rank, score}` but also set the matching named field (`rrf_score` or `borda_score`) so trail/API aren't mysteriously null. Hybrid unchanged.

---

## Nice (ship if cheap)

### N1 — `py.typed` marker in package
### N2 — `GET /v1/boardroom` list (limit default 20, max 100; newest first; auth same as other `/v1`)
### N3 — Harden `extract_json` with `JSONDecoder.raw_decode` fallback after fence strip
### N4 — `PROTOCOL_VERSION = "polygnosis-v3"` constant used by health/ready
### N5 — `SECURITY.md` (dev-only open mode; report vulns via GitHub issues)
### N6 — Global LLM concurrency semaphore (`POLYGNOSIS_MAX_LLM_CONCURRENCY` default 8) wrapping `complete`
### N7 — CONTRIBUTING.md short (dev setup, ruff, pytest, branch tips)

---

## Version / docs

- Bump to `0.3.0` in `pyproject.toml` / `__init__` fallback
- Update README + SPEC for new endpoints/fields/env knobs
- Extend `.env.example`
- Add tests for: lease reclaim, atomic 429, compare_digest path (401), include_solutions, list endpoint, timings.json, heterogeneity warning, extract_json harden, ready job counts

## Deferred still
Hermes/Eve, Redis/Postgres fabric, cancel/TTL endpoints, artifact download ZIP, mid-pipeline resume, CORS, live gateway CI, relicense.

---

## Parallel ownership

| Owner | Scope |
|-------|--------|
| A | jobs.py leases + WAL + create_if_capacity + count_by_status; tests/test_jobs.py |
| B | llm.py pool + temp/max_tokens + semaphore + extract_json; tests/test_llm.py |
| C | schemas.py (objective, include_solutions, list response); config.py + default_config.yaml + config.yaml temps; py.typed; ruff/pyproject 0.3.0; .gitignore dist; delete requirements.txt |
| D | pipeline.py heterogeneity, timings, role temps/tokens, ranking shape; prompts untouched unless needed |
| E | main.py auth hmac, atomic create, shutdown race, middleware, ready counts, list route, config cache, lifespan client close |
| F | tests (api/pipeline/config), README, SPEC, SECURITY.md, CONTRIBUTING.md, .env.example, CI if needed |

Build order hint: A∥B∥C first, then D∥E, then F. If parallel, respect file ownership strictly.
