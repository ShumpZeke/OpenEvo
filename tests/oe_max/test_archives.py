"""
Research archives.

Each archive answers a question a single best-score cannot, so the tests focus
on the cases where a naive implementation quietly loses information.
"""
import math
import pytest

from oe_max.archives import (
    ArchiveSet, Entry, FailureArchive, HallOfFame, NoveltyArchive, ParetoArchive,
)


def e(cid, score=None, **kw):
    metrics = kw.pop("metrics", None) or ({"combined_score": score} if score is not None else {})
    return Entry(candidate_id=cid, metrics=metrics, **kw)


# ------------------------------------------------------- hall of fame
def test_records_each_new_champion_in_order():
    h = HallOfFame()
    assert h.consider(e("a", 1.0))
    assert h.consider(e("b", 1.5))
    assert not h.consider(e("c", 1.2))      # not an improvement
    assert [x.candidate_id for x in h.entries] == ["a", "b"]
    assert h.champion.candidate_id == "b"


def test_deposed_champions_are_kept():
    """A candidate that held the crown is history, even once beaten."""
    h = HallOfFame()
    for i, s in enumerate([1.0, 1.1, 1.2, 1.3]):
        h.consider(e(f"c{i}", s))
    assert len(h.entries) == 4
    assert h.progression()[0][1] == 1.0


def test_entries_without_the_metric_are_ignored():
    h = HallOfFame()
    assert not h.consider(e("x", metrics={"other": 5.0}))
    assert h.champion is None


# ------------------------------------------------------------ pareto
def test_dominated_candidate_is_rejected():
    p = ParetoArchive({"accuracy": True, "latency": False})
    assert p.consider(e("good", metrics={"accuracy": 0.9, "latency": 10}))
    # worse on both axes
    assert not p.consider(e("worse", metrics={"accuracy": 0.8, "latency": 20}))
    assert len(p.front()) == 1


def test_trade_off_candidates_both_survive():
    """The whole point: less accurate but much faster is still interesting."""
    p = ParetoArchive({"accuracy": True, "latency": False})
    p.consider(e("accurate", metrics={"accuracy": 0.95, "latency": 100}))
    p.consider(e("fast", metrics={"accuracy": 0.85, "latency": 5}))
    assert {x.candidate_id for x in p.front()} == {"accurate", "fast"}


def test_new_candidate_evicts_those_it_dominates():
    p = ParetoArchive({"accuracy": True, "latency": False})
    p.consider(e("old", metrics={"accuracy": 0.8, "latency": 50}))
    p.consider(e("better", metrics={"accuracy": 0.9, "latency": 40}))
    assert [x.candidate_id for x in p.front()] == ["better"]


def test_minimised_objective_respected():
    p = ParetoArchive({"latency": False})
    p.consider(e("slow", metrics={"latency": 100}))
    p.consider(e("fast", metrics={"latency": 10}))
    assert [x.candidate_id for x in p.front()] == ["fast"]


def test_missing_or_nan_metric_rejected_not_crashed():
    p = ParetoArchive({"accuracy": True})
    assert not p.consider(e("no-metric", metrics={"other": 1}))
    assert not p.consider(e("nan", metrics={"accuracy": float("nan")}))
    assert p.rejected == 2


def test_strictly_improving_sequence_collapses_to_one_point():
    """
    Higher `a` AND lower `b` each time means every entry dominates its
    predecessor, so the front is correctly a single point — not a trade-off.
    """
    p = ParetoArchive({"a": True, "b": False}, capacity=5)
    for i in range(12):
        p.consider(e(f"c{i}", metrics={"a": i / 12, "b": 1 - i / 12}))
    assert [x.candidate_id for x in p.front()] == ["c11"]


def test_capacity_trims_the_crowded_middle_not_the_extremes():
    """
    A genuine front: more `a` costs more `b`, so nothing dominates anything.
    Trimming to capacity must thin the crowded middle and keep the extremes,
    because the endpoints are the most informative points on a front.
    """
    p = ParetoArchive({"a": True, "b": False}, capacity=5)
    for i in range(12):
        # a rises (better) while b also rises (worse) — a real trade-off.
        p.consider(e(f"c{i}", metrics={"a": i / 12, "b": i / 12}))
    assert len(p.front()) <= 5
    vals = sorted(x.metrics["a"] for x in p.front())
    assert vals[0] < 0.2 and vals[-1] > 0.8, "extremes of the front were lost"


def test_objectives_required():
    with pytest.raises(ValueError):
        ParetoArchive({})


# ----------------------------------------------------------- novelty
def test_empty_archive_is_maximally_novel():
    n = NoveltyArchive()
    assert math.isinf(n.novelty([0.0, 0.0]))


def test_distant_behaviour_scores_more_novel_than_nearby():
    n = NoveltyArchive(k=2)
    for i in range(5):
        n.consider(e(f"c{i}", behaviour=[i * 0.01, 0.0]))
    assert n.novelty([10.0, 10.0]) > n.novelty([0.02, 0.0])


def test_threshold_rejects_unremarkable_behaviour():
    n = NoveltyArchive(k=1, threshold=1.0)
    n.consider(e("a", behaviour=[0.0, 0.0]))
    accepted, _ = n.consider(e("b", behaviour=[0.01, 0.0]))
    assert not accepted
    accepted, _ = n.consider(e("c", behaviour=[5.0, 5.0]))
    assert accepted


def test_entry_without_behaviour_is_skipped():
    n = NoveltyArchive()
    accepted, score = n.consider(e("x", 1.0))
    assert not accepted and score == 0.0


# ---------------------------------------------------------- failures
def test_failures_are_indexed_by_reason_and_operator():
    f = FailureArchive()
    f.record(e("a", operator="RADICAL_RETHINK"), "syntax error")
    f.record(e("b", operator="RADICAL_RETHINK"), "timeout")
    f.record(e("c", operator="LOCAL_OPTIMIZE"), "syntax error")
    d = f.to_dict()
    assert d["by_reason"]["syntax error"] == 2
    assert d["by_operator"]["RADICAL_RETHINK"] == 2


def test_already_failed_is_a_cheap_precheck():
    """At ~130s per generation, not re-deriving a known failure is the point."""
    f = FailureArchive()
    f.record(e("a", code_hash="deadbeef"), "crashed")
    assert f.already_failed("deadbeef")
    assert not f.already_failed("cafebabe")


def test_prompt_context_is_capped_and_deduplicated():
    f = FailureArchive()
    for i in range(50):
        f.record(e(f"c{i}"), "same failure")
    for i in range(5):
        f.record(e(f"d{i}"), f"distinct failure {i}")
    lines = f.recent_for_prompt(limit=3)
    assert len(lines) == 3
    assert len(set(lines)) == 3, "identical failures must not repeat in a prompt"


def test_prompt_context_filterable_by_operator():
    f = FailureArchive()
    f.record(e("a", operator="OP_A"), "failure a")
    f.record(e("b", operator="OP_B"), "failure b")
    lines = f.recent_for_prompt(limit=5, operator="OP_A")
    assert len(lines) == 1 and "failure a" in lines[0]


# --------------------------------------------------------- archive set
def test_archive_set_routes_one_candidate_to_every_archive():
    a = ArchiveSet(objectives={"combined_score": True, "latency": False})
    r = a.accept(Entry("c1", metrics={"combined_score": 1.0, "latency": 50},
                       behaviour=[0.0, 0.0]))
    assert r["new_champion"] and r["pareto_front"] and r["novel"]

    r2 = a.accept(Entry("c2", metrics={"combined_score": 0.5, "latency": 90},
                        behaviour=[0.0, 0.0]))
    assert not r2["new_champion"] and not r2["pareto_front"]


def test_archive_set_is_serialisable():
    import json
    a = ArchiveSet()
    a.accept(Entry("c1", metrics={"combined_score": 1.0}, behaviour=[1.0]))
    a.reject(Entry("c2", code_hash="abc", operator="LOCAL_OPTIMIZE"), "syntax error")
    json.dumps(a.to_dict())
    assert a.to_dict()["failures"]["size"] == 1
