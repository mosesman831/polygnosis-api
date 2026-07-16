# Production / OSS readiness plan

Status: **plan only** (no implementation yet). Approve milestones before build.

Derived from `SPEC.md`, the current `src/polygnosis_api` tree, and an architecture review of what actually blocks a trustworthy public release.

## Verdict

The **boardroom protocol is real**. Phases 0–6, RRF/Borda/hybrid, and the POST→poll shape match the SPEC. `consensus.py` is the strongest part of the repo.

The **service wrapper is prototype-grade**. Authless spend, process-local jobs, silent degradation marked `completed`, and a broken `pip install` path are the things that would embarrass a public OSS deploy. Fix those before polishing prompts or adding agent backends.

Do **not** rewrite: `consensus.py`, the phase machine in `BoardroomPipeline`, or the external POST `202` + poll choreography. Evolve under that API.

---

## What already works (keep)

| Area | Why keep it |
|------|-------------|
| `consensus.py` + `tests/test_consensus.py` | Formulas match SPEC; unit-tested |
| Phase layout 0→6 in `pipeline.py` | Faithful to PolyGnosis v3 |
| Async job shape (`POST` / `GET`) | Right product shape for multi-minute runs |
| Thin `LLMClient.complete` | Right thickness; wrap with retries, don't invent an SDK |
| Module split (`prompts`, `personas`, `reflexion`, `schemas`) | Clear ownership |
| GPL-3.0 | Matches upstream PolyGnosis; intentional |

---

## Milestones (build in this order)

### M0 — Honest packaging and versioning (OSS installability)

**Goal:** `pip install .` / wheel install works without a source checkout ritual.

| Work | Detail |
|------|--------|
| Ship `config.yaml` | Package data or CWD/`POLYGNOSIS_CONFIG_PATH` discovery that works from `site-packages` |
| Fix `PACKAGE_ROOT` | Stop assuming `Path(__file__).parents[2]` is the repo root |
| Single version source | One version in `pyproject.toml`; import it in `main` / `__init__` |
| Align docs | README documents `polygnosis-api` entrypoint *and* env overrides; drop conflicting `--app-dir`-only story as the only path |
| Drop pytest from runtime `requirements.txt` | Keep it in `[project.optional-dependencies] dev` only |
| Drop dead knobs | Unused YAML `artifacts_dir`, or wire it; decide on unused `acomplete` |

**Exit:** Fresh venv, `pip install .`, `polygnosis-api --help` / load config without `FileNotFoundError`. Version string matches package once.

---

### M1 — Public-service safety (auth + backpressure)

**Goal:** Binding `0.0.0.0` does not create an unbounded spend faucet.

| Work | Detail |
|------|--------|
| Inbound API key | Require `Authorization: Bearer …` (or `X-API-Key`) for `POST` and `GET` job routes; separate from outbound `POLYGNOSIS_API_KEY` |
| Document keys | `.env.example` names both: operator gateway key vs service access key |
| Max in-flight jobs | Semaphore / queue depth; reject with `429` when full |
| Request bounds | Cap `objective` length; keep `solver_count` 2–5 |
| CORS / security headers | Only if browser clients are in scope; otherwise document CLI/server-to-server |

**Exit:** Unauthenticated `POST` returns `401`. Saturated server returns `429`. Authenticated happy path unchanged.

Auth and durable jobs gate the **same** production milestone (see M2). Implement as separate PRs if useful, but do not call the service “public deploy ready” until both land.

---

### M2 — Durable jobs (replace process-local store)

**Goal:** Restart, deploy, and single-replica recovery do not erase the POST→poll contract.

| Work | Detail |
|------|--------|
| Durable job record | SQLite first (OSS-friendly default); schema: `id`, `status`, `phase`, `detail`, `request`, `result`, `error`, timestamps |
| Worker model | Background worker that claims jobs (lease / `FOR UPDATE`-style), not `daemon=True` fire-and-forget threads |
| Persist phase progress | Poll shows real phase after process bounce if worker resumes or marks failed cleanly |
| Artifact link | Keep writing `{artifacts_dir}/{job_id}/`; store path on the job row; do **not** treat artifacts alone as the job store |
| Single-worker default | Document `workers=1` until a shared store + external worker exists; refuse multi-worker with in-memory (or remove in-memory entirely) |
| Optional later | Redis/Postgres for multi-instance; cancel + TTL endpoints |

**Exit:** Kill the API mid-run → job ends `failed` or resumes; completed jobs still pollable after restart. Artifacts still on disk.

---

### M3 — Honest results (degradation is visible)

**Goal:** A “completed” boardroom means consensus actually ran. Soft failures are labeled.

| Work | Detail |
|------|--------|
| Phase outcomes | Each phase records `ok` / `degraded` / `failed` with reason (empty LLM, non-JSON, dead solvers) |
| Empty ranking ≠ clean success | Missing `rankings` after scoring → job `failed` or `completed` with `degraded: true` + explicit `warnings[]` (pick one contract and stick to it; prefer fail or degrade, never silent `{}`) |
| Quality gate fail-closed | Non-JSON / empty gate → do **not** default `PASS`; treat as `UNKNOWN`/`ERROR` and either fail the job or keep synthesis with a loud warning |
| Expose Layer-1 `scoring` | Add `scoring` to `BoardroomResult` (or a nested typed model); stop silently dropping it |
| Early-resolution parity | Write synthetic `scoring.json`; include `rrf_score`/`borda_score`/`avg_rank` or document a distinct early-res ranking shape |
| Typed `consensus_ranking` | Replace `dict[str, Any]` with a real model so hybrid vs rrf/borda shapes are honest |
| Drop or gate `artifacts_dir` | Server filesystem path is not a public resource; admin-only or omit from default response |

**Exit:** Forced scorer failure and forced gate non-JSON are covered by tests and produce a non-silent client-visible outcome.

---

### M4 — LLM resilience

**Goal:** Transient gateway errors do not silently delete solvers or collapse scoring.

| Work | Detail |
|------|--------|
| Retries | Retry 429/5xx/timeouts with backoff on `LLMClient.complete` |
| Error classification | Distinguish transport failure vs empty content vs bad JSON at the call site |
| Timeouts | Keep per-phase YAML timeouts; document them |
| Quorum messaging | When solvers die from transport, surface count + reason in job `detail` / warnings |

**Exit:** Simulated 500 on one call recovers; persistent failure still degrades with M3 honesty.

---

### M5 — Test depth (what SPEC success criteria actually need)

**Goal:** CI protects the protocol, not just health.

| Work | Detail |
|------|--------|
| Fake `LLMClient` | Inject canned JSON per phase; exercise full `BoardroomPipeline.run` |
| Cases | Quorum failure; critique non-JSON stub; scoring empty → degraded; quality gate FAIL fallback; early resolution path; auth 401; job persist round-trip |
| Consensus edges | Axis ties, missing axes, `rrf`/`borda` response shapes, custom `k` |
| Packaging smoke | Import + config load from installed layout (or path override test) |
| CI | GitHub Actions: `ruff` + `pytest` on PR |

**Exit:** SPEC success criteria that can be automated without a live gateway are green in CI. Live gateway e2e stays optional/manual or a marked integration job.

---

### M6 — Product polish (after M0–M5)

Do these only once the service is trustworthy:

| Work | Detail |
|------|--------|
| Orchestrator gets `solver_count` | Persona count matches parallel solve |
| Dedicated `models.scorer` | Stop reusing synthesizer for Layer-1 scores |
| Reflexion scope | Default **off** for multi-tenant; or per-job / per-key buffer. Global `.corrections_buffer.json` is wrong for a public host |
| Liveness vs readiness | `/health` liveness; `/ready` checks config + key present (optional: probe gateway) |
| Artifact retention | TTL / cleanup job; cap disk growth |
| Structured logging | `job_id` on every log line |
| Graceful shutdown | Stop accepting jobs; wait or fail in-flight with durable status |
| Version / OpenAPI | Security schemes in OpenAPI; document poll semantics |

**Out of scope for first production cut (SPEC non-goals / future):**

- Hermes / Eve live-tool agent sessions
- Sub-second claim verification
- Full multi-region queue fabric

When agent backends return, add a `Solver`/`Executor` protocol seam and swap implementations. Do not redesign the phase machine for that today.

---

## Suggested PR sequence

1. **M0** packaging + version + docs/requirements cleanup  
2. **M5 slice** (fake LLM + pipeline unit tests) in parallel with M0 if capacity allows  
3. **M1** inbound auth + in-flight limit  
4. **M2** SQLite job store + worker  
5. **M3** honest degradation + schema fixes  
6. **M4** LLM retries  
7. **M5** remaining CI + edge tests  
8. **M6** polish items as separate small PRs  

Ship a **0.x** or keep `1.0.0` only after M0–M5. Calling the current tree “shipping / 1.0” oversells readiness.

---

## Spec delta checklist (when building)

Update `SPEC.md` / README when code lands:

- [ ] Inbound auth model  
- [ ] Job durability + restart semantics  
- [ ] Degraded vs completed vs failed  
- [ ] `scoring` in HTTP response  
- [ ] Early-resolution ranking / artifact rules  
- [ ] Quality gate fail-closed behavior  
- [ ] Reflexion default (off for public)  
- [ ] Install paths (`pip install` + config discovery)  
- [ ] Versioning policy (0.x until M5)  

---

## Explicit non-goals for this plan

- Rewriting consensus math  
- Redesigning the seven-phase protocol  
- Building agent sandboxes  
- Replacing GPL with a more permissive license (would diverge from PolyGnosis)  
