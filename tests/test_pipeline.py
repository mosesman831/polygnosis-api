"""Full-pipeline tests driven by a canned FakeLLM (no network)."""

from __future__ import annotations

import json

import pytest

from polygnosis_api.pipeline import BoardroomPipeline
from polygnosis_api.reflexion import ReflexionBuffer


class FakeLLM:
    """Returns canned text per call, keyed by the ``label`` the pipeline passes.

    Exact-label matches win; otherwise the first prefix match is used (so a
    single ``critic`` entry covers ``critic-A``/``critic-B``/...). Unknown
    labels return "" which the pipeline treats as an empty/dead completion.
    """

    def __init__(self, by_label: dict[str, str]):
        self.by_label = by_label
        self.calls: list[str] = []

    def complete(
        self,
        prompt,
        model=None,
        *,
        temperature=0.3,
        timeout=300.0,
        label="llm",
        max_tokens=None,
    ):
        self.calls.append(label)
        if label in self.by_label:
            return self.by_label[label]
        for key, value in self.by_label.items():
            if label.startswith(key):
                return value
        return ""


def _scoring_json(order: list[str]) -> str:
    """Build a scorer response ranking ``order`` best→worst on every axis."""
    rankings = []
    for i, sid in enumerate(order):
        score = 9 - i
        rankings.append(
            {
                "solution_id": sid,
                "solver_label": sid,
                "scores": {
                    "correctness": score,
                    "efficiency": score,
                    "maintainability": score,
                    "robustness": score,
                    "security": score,
                },
                "total": score * 5,
            }
        )
    return json.dumps({"rankings": rankings})


def _responses(**over: str) -> dict[str, str]:
    base = {
        "orchestrator": json.dumps(
            {
                "problem_statement": "PS",
                "success_criteria": ["Correctness"],
                "domain": "general",
                "personas": ["Expert A", "Expert B", "Expert C"],
            }
        ),
        "quorum-judge": json.dumps({"unanimous": False, "confidence": 0.0}),
        "solver-A": "SOLUTION A",
        "solver-B": "SOLUTION B",
        "solver-C": "SOLUTION C",
        "critic": json.dumps(
            {"solution_id": "s", "overall_grade": "PASS", "score": 80}
        ),
        "scorer": _scoring_json(["s0", "s1", "s2"]),
        "synthesizer": "SYNTHESIS",
        "quality-gate": json.dumps({"verdict": "PASS", "reasoning": "good"}),
        "meta-reviewer": "META REVIEW",
    }
    base.update(over)
    return base


def _cfg(
    *, solver_model_ids: tuple[str, ...] = ("m1", "m2", "m3"), **settings_over
) -> dict:
    """Build a boardroom config for the FakeLLM pipeline.

    Solvers get *distinct* model ids by default so the happy path is
    heterogeneous (not degraded). Pass ``solver_model_ids`` with repeats to
    exercise the S3 heterogeneity warning.
    """
    settings = {
        "solver_count": 3,
        "min_solvers_for_quorum": 2,
        "early_resolution_enabled": False,
        "quality_gate_enabled": True,
        "max_debate_rounds": 1,
        "scoring_algorithm": "hybrid",
        "rrf_k": 60,
    }
    settings.update(settings_over)
    models = {role: "m" for role in (
        "orchestrator", "critic", "synthesizer", "scorer", "meta_reviewer", "fallback"
    )}
    for i, model_id in enumerate(solver_model_ids):
        models[f"solver_{i + 1}"] = model_id
    return {"models": models, "solver_models": [], "settings": settings}


def _pipeline(tmp_path, cfg: dict, responses: dict[str, str]) -> BoardroomPipeline:
    return BoardroomPipeline(
        cfg=cfg,
        llm=FakeLLM(responses),
        reflexion=ReflexionBuffer(str(tmp_path / "buf.json"), enabled=False),
        artifacts_root=tmp_path,
    )


def test_happy_path_hybrid_ranking(tmp_path):
    pipeline = _pipeline(tmp_path, _cfg(), _responses())
    result = pipeline.run("obj", job_id="happy")

    assert result["degraded"] is False
    assert result["consensus_ranking"]
    entry = next(iter(result["consensus_ranking"].values()))
    assert "avg_rank" in entry
    assert "rrf_score" in entry
    assert "borda_score" in entry
    assert result["scoring_algorithm"] == "hybrid"
    assert result["final_output"] == "SYNTHESIS"
    assert result["scoring"]["_consensus_ranking"]


def test_scoring_empty_raises(tmp_path):
    responses = _responses(scorer=json.dumps({"rankings": []}))
    pipeline = _pipeline(tmp_path, _cfg(), responses)
    with pytest.raises(RuntimeError):
        pipeline.run("obj", job_id="empty-scoring")


def test_quality_gate_non_json_degrades(tmp_path):
    responses = _responses(**{"quality-gate": "This is not JSON"})
    pipeline = _pipeline(tmp_path, _cfg(), responses)
    result = pipeline.run("obj", job_id="gate-error")

    assert result["degraded"] is True
    assert result["warnings"]
    assert result["quality_gate"]["verdict"] == "ERROR"
    # ERROR keeps the synthesis as final output.
    assert result["final_output"] == "SYNTHESIS"


def test_quality_gate_fail_falls_back_to_top_individual(tmp_path):
    # Rank s2 (solver-C) first so the top individual is deterministic.
    responses = _responses(
        scorer=_scoring_json(["s2", "s1", "s0"]),
        **{"quality-gate": json.dumps({"verdict": "FAIL", "reasoning": "regressed"})},
    )
    pipeline = _pipeline(tmp_path, _cfg(), responses)
    result = pipeline.run("obj", job_id="gate-fail")

    assert result["consensus_ranking"]["s2"]["rank"] == 1
    assert result["final_output"] == "SOLUTION C"


def test_early_resolution_writes_synthetic_ranking(tmp_path):
    responses = _responses(
        **{"quorum-judge": json.dumps({"unanimous": True, "confidence": 0.95})}
    )
    pipeline = _pipeline(tmp_path, _cfg(early_resolution_enabled=True), responses)
    result = pipeline.run("obj", job_id="early")

    assert result["early_resolution"] is True
    entry = result["consensus_ranking"]["s0"]
    assert entry["note"] == "early_resolution"
    assert entry["rank"] == 1
    assert entry["avg_rank"] == 1.0
    assert result["scoring"]["_early_resolution"] is True
    assert (tmp_path / "early" / "scoring.json").exists()


def test_below_quorum_raises(tmp_path):
    responses = _responses(
        **{"solver-A": "", "solver-B": "", "solver-C": ""}
    )
    pipeline = _pipeline(tmp_path, _cfg(), responses)
    with pytest.raises(RuntimeError):
        pipeline.run("obj", job_id="no-quorum")


def test_timings_json_written_after_run(tmp_path):
    # S5: each phase records a wall-clock duration flushed to timings.json.
    pipeline = _pipeline(tmp_path, _cfg(), _responses())
    result = pipeline.run("obj", job_id="timed")

    timings_path = tmp_path / "timed" / "timings.json"
    assert timings_path.exists()
    timings = json.loads(timings_path.read_text())
    # The core phases should all have a recorded, non-negative duration.
    for phase in ("orchestrate", "solve", "scoring", "synthesis", "meta_review"):
        assert phase in timings
        assert timings[phase] >= 0
    # The in-result copy mirrors the file.
    assert result["phase_timings"] == timings


def test_heterogeneity_warning_when_solvers_share_model(tmp_path):
    # S3: two solvers resolving to the same model id → warning + degraded solve.
    cfg = _cfg(solver_model_ids=("dup", "dup", "solo"))
    pipeline = _pipeline(tmp_path, cfg, _responses())
    result = pipeline.run("obj", job_id="hetero")

    assert result["degraded"] is True
    assert any(
        "not fully heterogeneous" in w for w in result["warnings"]
    ), result["warnings"]
    solve_outcome = next(
        o for o in result["phase_outcomes"] if o["phase"] == "solve"
    )
    assert solve_outcome["status"] == "degraded"


def test_include_solutions_false_nulls_trail(tmp_path):
    # S9: full solution text is stripped from the trail when not requested.
    pipeline = _pipeline(tmp_path, _cfg(), _responses())
    result = pipeline.run("obj", job_id="no-sol", include_solutions=False)

    assert result["trail"]
    assert all(item["solution"] is None for item in result["trail"])


def test_include_solutions_true_keeps_trail(tmp_path):
    # S9: full solution text stays in the trail when explicitly requested.
    pipeline = _pipeline(tmp_path, _cfg(), _responses())
    result = pipeline.run("obj", job_id="with-sol", include_solutions=True)

    assert result["trail"]
    solutions = {item["solution"] for item in result["trail"]}
    assert solutions == {"SOLUTION A", "SOLUTION B", "SOLUTION C"}
