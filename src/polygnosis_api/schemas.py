"""Pydantic request/response schemas for the PolyGnosis API."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class ScoringAlgorithm(StrEnum):
    rrf = "rrf"
    borda = "borda"
    hybrid = "hybrid"


class JobStatus(StrEnum):
    queued = "queued"
    running = "running"
    completed = "completed"
    completed_degraded = "completed_degraded"
    failed = "failed"


class ConsensusEntry(BaseModel):
    """A single solver's placement in the consensus ranking."""

    rank: int
    avg_rank: float | None = None
    rrf_score: float | None = None
    borda_score: float | None = None
    score: float | None = None
    note: str | None = None


class PhaseOutcome(BaseModel):
    """Outcome record for a single pipeline phase."""

    phase: str
    status: Literal["ok", "degraded", "failed"]
    detail: str | None = None


class BoardroomRequest(BaseModel):
    # No max_length here: main enforces settings.objective_max_chars as the
    # authoritative limit and returns 422 when exceeded.
    objective: str = Field(
        ...,
        min_length=1,
        description="High-stakes problem to solve",
    )
    scoring_algorithm: ScoringAlgorithm | None = Field(
        default=None,
        description="Override config: rrf | borda | hybrid",
    )
    solver_count: int | None = Field(default=None, ge=2, le=5)
    early_resolution: bool | None = None
    quality_gate: bool | None = None
    max_debate_rounds: int | None = Field(default=None, ge=1, le=5)
    include_solutions: bool = Field(
        default=False,
        description="Include full solver solution text in the HTTP trail (artifacts always retain it)",
    )


class BoardroomCreateResponse(BaseModel):
    job_id: str
    status: JobStatus
    poll_url: str


class SolverTrailItem(BaseModel):
    solution_id: str
    solver: str
    model: str | None = None
    tool_class: str | None = None
    rank: int | None = None
    rrf_score: float | None = None
    borda_score: float | None = None
    avg_rank: float | None = None
    critic_score: int | float | None = None
    critic_grade: str | None = None
    solution: str | None = None


class BoardroomResult(BaseModel):
    job_id: str
    objective: str
    domain: str | None = None
    problem_statement: str | None = None
    success_criteria: list[str] = []
    personas: list[str] = []
    early_resolution: bool = False
    scoring_algorithm: str = "hybrid"
    # Prefer typed ConsensusEntry values; pydantic coerces plain dicts into
    # ConsensusEntry, so callers may still pass raw dicts.
    consensus_ranking: dict[str, ConsensusEntry] = {}
    quality_gate: dict[str, Any] | None = None
    final_output: str | None = None
    meta_review: str | None = None
    trail: list[SolverTrailItem] = []
    reflexion_buffer_size: int = 0
    scoring: dict[str, Any] | None = None
    warnings: list[str] = []
    degraded: bool = False
    phase_outcomes: list[PhaseOutcome] = []


class BoardroomJobResponse(BaseModel):
    job_id: str
    status: JobStatus
    phase: str | None = None
    detail: str | None = None
    error: str | None = None
    created_at: str
    updated_at: str
    result: BoardroomResult | None = None


class BoardroomListResponse(BaseModel):
    jobs: list[BoardroomJobResponse] = []
    count: int = 0


class HealthResponse(BaseModel):
    status: str
    version: str
    protocol: str = "polygnosis-v3"


class ReadyResponse(BaseModel):
    status: str
    version: str
    config_loaded: bool
    gateway_key_configured: bool
    auth_required: bool
    jobs: dict[str, int] = {}
