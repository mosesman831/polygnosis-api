# PolyGnosis API

> **LatticeAG · Poly series** · Status: **v0.2 build target** — see `docs/BUILD_SPEC.md`
> **Type:** Public service API
> **Protocol:** PolyGnosis v3 (full boardroom)
> **Extract target:** `mosesman831/polygnosis-api`

## Problem

Single-model answers have no reliability signal. For high-stakes objectives (architecture, security-sensitive code, correctness-critical designs), you need adversarial multi-model consensus — not one completion.

## Relationship to PolyGnosis

[PolyGnosis](https://github.com/mosesman831/PolyGnosis) is the Hermes Agent skill that runs the boardroom via `hermes chat` agent sessions with asymmetric tool allocation.

**This repo** exposes the **same seven-phase protocol** as an HTTP API:

- Model-only solvers (OpenAI-compatible gateway) — no Hermes dependency
- Async jobs (`POST` → poll `GET`) because full runs take minutes
- Formal **RRF + Borda** (hybrid default) consensus ranking — not rule-of-thumb scores
- Durable SQLite job store, optional inbound service API key, honest degraded/failed statuses

## Solution

```
POST /v1/boardroom
Authorization: Bearer <POLYGNOSIS_SERVICE_API_KEY>
{
  "objective": "Design a production-grade JWT auth middleware in Rust",
  "scoring_algorithm": "hybrid",
  "solver_count": 3
}
→ 202 { "job_id": "...", "poll_url": "/v1/boardroom/..." }

GET /v1/boardroom/{job_id}
→
{
  "status": "completed",
  "result": {
    "final_output": "...",
    "meta_review": "...",
    "scoring_algorithm": "hybrid",
    "scoring": { "rankings": [...], "_consensus_ranking": {...} },
    "consensus_ranking": {
      "s1": {"rank": 1, "avg_rank": 1.0, "rrf_score": 0.081, "borda_score": 8},
      "s0": {"rank": 2, "avg_rank": 2.0, "rrf_score": 0.079, "borda_score": 6}
    },
    "quality_gate": {"verdict": "PASS", ...},
    "warnings": [],
    "degraded": false,
    "phase_outcomes": [{"phase": "scoring", "status": "ok", "detail": null}, ...],
    "trail": [ { "solver": "...", "rank": 1, "solution": "..." }, ... ]
  }
}
```

Statuses: `queued` | `running` | `completed` | `completed_degraded` | `failed`.

## Full workflow (must match PolyGnosis v3)

```
0   Orchestrate          → problem statement + dynamic personas (count = solver_count)
1   Parallel solve       → 3+ heterogeneous models, persona lenses
1.5 Early resolution     → skip critique+scoring on unanimous quorum
2   Critique + Reflexion → adversarial review, revise rounds, failure buffer (opt-in)
3   Formal scoring       → LLM 5-axis scores (scorer model) → RRF / Borda / hybrid
4   Synthesis            → unified solution from ranked elements
5   Quality gate         → FAIL → top individual; ERROR (non-JSON) → keep synthesis + degrade
6   Meta-review          → human-readable consensus explanation
```

### Layer-2 consensus (required)

```
RRF(s)   = Σ_axis  1 / (k + rank_axis(s))     # k = 60; ties broken by solution_id
Borda(s) = Σ_axis  (n - 1 - rank_axis(s))
Hybrid   = avg(RRF_rank(s), Borda_rank(s))    # default; ties by solution_id
```

LLM scores are **inputs only**. Final winner is determined by the deterministic algorithm.

Empty Layer-1 rankings (non-early path) → job **failed** (not a silent empty consensus).

## Key design decisions

- **Async by default.** Full boardroom is multi-minute; sync HTTP is the wrong shape.
- **Durable jobs.** SQLite job records + background worker (not per-request daemon threads).
- **Inbound auth optional-but-recommended.** Empty `POLYGNOSIS_SERVICE_API_KEY` = open local mode with warning; set key for any public bind.
- **Model-only v1.** No agent sandboxes. Persona tool classes are prompt constraints.
- **Hybrid scoring default.** Most resilient when RRF and Borda disagree.
- **Honest degradation.** Soft failures surface as `completed_degraded` + `warnings`, or `failed`.
- **Artifacts on disk.** Every job writes a run directory; paths are not exposed on the public HTTP result.
- **Reflexion opt-in.** Cross-run buffer default off (not multi-tenant safe).

## Non-goals (v0.2)

- Hermes / Eve agent sessions with live tools
- Sub-second claim verification
- Multi-instance Redis/Postgres job fabric
- Replacing LexVerdict-style post-tool checks
- Mid-pipeline crash resume

## Success criteria

- Consensus ranking unit tests match RRF / Borda / hybrid formulas
- Config loads from packaged default after `pip install`
- Inbound auth returns 401 when service key configured and missing
- Jobs survive API process restart (SQLite)
- Empty scoring fails the job; quality-gate non-JSON yields `ERROR` + degraded
- Response includes `scoring` and `consensus_ranking` with hybrid score fields
- Quality gate `FAIL` falls back to top individual solution
- CI runs ruff + pytest

## References

1. Cormack et al. (2009) — Reciprocal Rank Fusion
2. de Borda (1781) — Borda Count
3. Shinn et al. (2023) — Reflexion
4. PolyGnosis formal spec — https://github.com/mosesman831/PolyGnosis/blob/main/POLYGNOSIS_SPEC.md
5. Implementation freeze — `docs/BUILD_SPEC.md`
