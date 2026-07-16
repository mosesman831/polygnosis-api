"""Unit tests for RRF / Borda / hybrid consensus ranking."""

from polygnosis_api.consensus import (
    borda_rank,
    compute_consensus_ranking,
    hybrid_rank,
    rrf_rank,
)


def _sample_scores():
    # s0 wins correctness+security, s1 wins efficiency+maintainability, s2 wins robustness
    return [
        {
            "solution_id": "s0",
            "solver_label": "A",
            "scores": {
                "correctness": 9,
                "efficiency": 5,
                "maintainability": 5,
                "robustness": 6,
                "security": 9,
            },
        },
        {
            "solution_id": "s1",
            "solver_label": "B",
            "scores": {
                "correctness": 7,
                "efficiency": 9,
                "maintainability": 9,
                "robustness": 5,
                "security": 6,
            },
        },
        {
            "solution_id": "s2",
            "solver_label": "C",
            "scores": {
                "correctness": 6,
                "efficiency": 6,
                "maintainability": 6,
                "robustness": 9,
                "security": 5,
            },
        },
    ]


def test_rrf_rank_order_and_formula():
    ranked = rrf_rank(_sample_scores(), k=60)
    by_id = dict(ranked)
    # Each axis: rank1 → 1/61, rank2 → 1/62, rank3 → 1/63
    # s0: corr1, eff3, maint3, rob2, sec1
    expected_s0 = 2 * (1 / 61) + (1 / 62) + 2 * (1 / 63)
    # s1: corr2, eff1, maint1, rob3, sec2
    expected_s1 = 2 * (1 / 61) + 2 * (1 / 62) + (1 / 63)
    # s2: corr3, eff2, maint2, rob1, sec3
    expected_s2 = (1 / 61) + 2 * (1 / 62) + 2 * (1 / 63)
    assert abs(by_id["s0"] - expected_s0) < 1e-9
    assert abs(by_id["s1"] - expected_s1) < 1e-9
    assert abs(by_id["s2"] - expected_s2) < 1e-9
    assert expected_s1 > expected_s0 > expected_s2
    assert [sid for sid, _ in ranked] == ["s1", "s0", "s2"]


def test_borda_rank_points():
    ranked = borda_rank(_sample_scores())
    # n=3 → points 2,1,0 per axis
    # s0: corr2 + eff0 + maint0 + rob1 + sec2 = 5
    # s1: corr1 + eff2 + maint2 + rob0 + sec1 = 6
    # s2: corr0 + eff1 + maint1 + rob2 + sec0 = 4
    by_id = dict(ranked)
    assert by_id["s1"] == 6
    assert by_id["s0"] == 5
    assert by_id["s2"] == 4
    assert ranked[0][0] == "s1"


def test_hybrid_averages_rank_positions():
    ranked = hybrid_rank(_sample_scores(), k=60)
    # RRF order: s1, s0, s2 → ranks 1,2,3
    # Borda order: s1, s0, s2 → ranks 1,2,3
    # avg: s1=1.0, s0=2.0, s2=3.0
    by_id = {sid: (avg, rrf, borda) for sid, avg, rrf, borda in ranked}
    assert by_id["s1"][0] == 1.0
    assert by_id["s0"][0] == 2.0
    assert by_id["s2"][0] == 3.0
    assert ranked[0][0] == "s1"


def test_compute_consensus_ranking_hybrid():
    scoring = {
        "rankings": [
            {
                "solution_id": s["solution_id"],
                "solver_label": s["solver_label"],
                "scores": s["scores"],
                "total": sum(s["scores"].values()),
            }
            for s in _sample_scores()
        ]
    }
    result = compute_consensus_ranking(scoring, algorithm="hybrid", k=60)
    assert result["s1"]["rank"] == 1
    assert result["s0"]["rank"] == 2
    assert result["s2"]["rank"] == 3
    assert "rrf_score" in result["s0"]
    assert "borda_score" in result["s0"]


def test_single_solution_edge_case():
    sols = [
        {
            "solution_id": "s0",
            "scores": {
                "correctness": 1,
                "efficiency": 1,
                "maintainability": 1,
                "robustness": 1,
                "security": 1,
            },
        }
    ]
    assert rrf_rank(sols) == [("s0", 1.0)]
    assert borda_rank(sols) == [("s0", 1.0)]
    assert hybrid_rank(sols) == [("s0", 1.0, 1.0, 1.0)]


def test_empty_scoring_returns_empty():
    assert compute_consensus_ranking({}, algorithm="hybrid") == {}
    assert compute_consensus_ranking(None, algorithm="rrf") == {}


def _tied_scores():
    # Two solutions with identical scores on every axis; a third clearly worse.
    # Provide out-of-order solution_ids to prove deterministic tie-breaking.
    return [
        {
            "solution_id": "s2",
            "scores": {a: 5 for a in ["correctness", "efficiency",
                                      "maintainability", "robustness", "security"]},
        },
        {
            "solution_id": "s1",
            "scores": {a: 5 for a in ["correctness", "efficiency",
                                      "maintainability", "robustness", "security"]},
        },
        {
            "solution_id": "s3",
            "scores": {a: 1 for a in ["correctness", "efficiency",
                                      "maintainability", "robustness", "security"]},
        },
    ]


def test_axis_ties_broken_by_solution_id_ascending():
    # For rrf/borda, tied axis scores must resolve to solution_id ascending,
    # regardless of the input order (s2 listed before s1).
    rrf = rrf_rank(_tied_scores(), k=60)
    borda = borda_rank(_tied_scores())
    # s1 and s2 tie on every axis; s1 must consistently outrank s2 (ascending id).
    rrf_order = [sid for sid, _ in rrf]
    borda_order = [sid for sid, _ in borda]
    assert rrf_order.index("s1") < rrf_order.index("s2")
    assert borda_order.index("s1") < borda_order.index("s2")
    # The worse solution s3 is always last.
    assert rrf_order[-1] == "s3"
    assert borda_order[-1] == "s3"


def test_axis_tie_determinism_independent_of_input_order():
    forward = rrf_rank(_tied_scores(), k=60)
    reversed_input = rrf_rank(list(reversed(_tied_scores())), k=60)
    assert [sid for sid, _ in forward] == [sid for sid, _ in reversed_input]


def test_missing_axis_defaults_to_zero():
    # s0 omits several axes entirely; missing scores default to 0.
    sols = [
        {"solution_id": "s0", "scores": {"correctness": 3}},
        {
            "solution_id": "s1",
            "scores": {a: 9 for a in ["correctness", "efficiency",
                                      "maintainability", "robustness", "security"]},
        },
    ]
    # Should not raise; s1 dominates because s0's missing axes are treated as 0.
    borda = dict(borda_rank(sols))
    rrf = dict(rrf_rank(sols))
    assert borda["s1"] > borda["s0"]
    assert rrf["s1"] > rrf["s0"]


def test_compute_consensus_ranking_rrf_shape():
    scoring = {
        "rankings": [
            {"solution_id": s["solution_id"], "scores": s["scores"]}
            for s in _sample_scores()
        ]
    }
    result = compute_consensus_ranking(scoring, algorithm="rrf", k=60)
    assert set(result) == {"s0", "s1", "s2"}
    for entry in result.values():
        assert set(entry) == {"rank", "score"}
        assert isinstance(entry["rank"], int)
        assert isinstance(entry["score"], float)
    assert result["s1"]["rank"] == 1


def test_compute_consensus_ranking_borda_shape():
    scoring = {
        "rankings": [
            {"solution_id": s["solution_id"], "scores": s["scores"]}
            for s in _sample_scores()
        ]
    }
    result = compute_consensus_ranking(scoring, algorithm="borda", k=60)
    for entry in result.values():
        assert set(entry) == {"rank", "score"}
    assert result["s1"]["rank"] == 1


def test_compute_consensus_ranking_custom_k():
    scoring = {
        "rankings": [
            {"solution_id": s["solution_id"], "scores": s["scores"]}
            for s in _sample_scores()
        ]
    }
    # A different k changes the raw rrf scores but keeps the ordering here.
    small_k = compute_consensus_ranking(scoring, algorithm="rrf", k=1)
    large_k = compute_consensus_ranking(scoring, algorithm="rrf", k=1000)
    assert small_k["s1"]["score"] != large_k["s1"]["score"]
    assert small_k["s1"]["rank"] == 1
    assert large_k["s1"]["rank"] == 1


def test_hybrid_tie_break_by_solution_id():
    # Two fully-tied solutions must order by solution_id ascending in hybrid.
    result = compute_consensus_ranking(
        {"rankings": [
            {"solution_id": s["solution_id"], "scores": s["scores"]}
            for s in _tied_scores()
        ]},
        algorithm="hybrid",
        k=60,
    )
    assert result["s1"]["rank"] < result["s2"]["rank"]
    assert result["s3"]["rank"] == 3
