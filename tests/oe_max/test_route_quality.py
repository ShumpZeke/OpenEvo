"""
Per-route mutation quality.

The risk this module exists to manage is a confident wrong recommendation:
switching the operator's chosen primary route on the strength of a handful of
lucky samples. Most of these tests are about refusing to do that.
"""
import json
import pytest

from oe_max.route_quality import (
    Attempt, RouteQualityTracker, RouteStats,
)


def att(route, **kw):
    return Attempt(route=route, **kw)


def feed(t, route, n, *, accepted=True, delta=0.1, latency_ms=1000.0,
         tokens=1000, parsed=True, passed_g0=True, passed_g1=True, operator=None):
    for _ in range(n):
        t.record(att(route, accepted=accepted, fitness_delta=delta,
                     latency_ms=latency_ms, tokens=tokens, parsed=parsed,
                     passed_g0=passed_g0, passed_g1=passed_g1, operator=operator))


# ------------------------------------------------------------ accounting
def test_unparseable_attempt_counted_and_not_credited():
    """
    The measured Ox Alpha failure mode: a request that returns 200 but yields no
    applicable diff. It must count against the route, not vanish.
    """
    t = RouteQualityTracker()
    feed(t, "a", 5, parsed=False)
    s = t.routes["a"]
    assert s.attempts == 5 and s.unparseable == 5
    assert s.accepted == 0 and s.parse_rate == 0.0
    assert s.validity_rate == 0.0


def test_gate_failures_are_attributed_separately():
    t = RouteQualityTracker()
    feed(t, "a", 3, passed_g0=False)
    feed(t, "a", 4, passed_g1=False)
    s = t.routes["a"]
    assert s.g0_failures == 3 and s.duplicates == 4
    assert s.accepted == 0
    assert s.duplicate_rate == pytest.approx(4 / 7)


def test_only_positive_deltas_count_as_improvement():
    t = RouteQualityTracker()
    t.record(att("a", accepted=True, fitness_delta=0.5))
    t.record(att("a", accepted=True, fitness_delta=0.0))
    t.record(att("a", accepted=True, fitness_delta=-0.3))
    s = t.routes["a"]
    assert s.accepted == 3, "all three entered the population"
    assert s.improvements == 1, "only one improved"
    assert s.total_positive_delta == pytest.approx(0.5)


def test_accepted_but_unimproved_still_counts_as_useful():
    """Widening the archive has value in a quality-diversity search."""
    a = Attempt("a", accepted=True, passed_g1=True, fitness_delta=0.0)
    assert a.useful and not a.improved


def test_reasoning_share_is_tracked():
    """Ox Alpha burned ~8k of an 8k budget on reasoning; that must be visible."""
    t = RouteQualityTracker()
    t.record(att("ox", tokens=8000, reasoning_tokens=7990, accepted=True,
                 fitness_delta=0.1))
    assert t.routes["ox"].reasoning_share == pytest.approx(7990 / 8000)


def test_zero_attempt_route_reports_none_not_zero_latency():
    s = RouteStats("empty")
    assert s.mean_latency_s is None
    assert s.reasoning_share is None
    assert s.improvement_per_second is None


# ------------------------------------------------------------ efficiency
def test_three_scarcity_views_can_disagree():
    """
    The central point of the module. A slow route with better mutations can win
    per-request and lose per-second — that is a real trade-off for the operator,
    not a contradiction to be averaged away.
    """
    t = RouteQualityTracker(min_attempts=5)
    # slow but good: 0.5 delta per attempt, 10s each
    feed(t, "slow_strong", 10, delta=0.5, latency_ms=10_000, tokens=8000)
    # fast but weak: 0.1 delta per attempt, 1s each
    feed(t, "fast_weak", 10, delta=0.1, latency_ms=1_000, tokens=1000)

    per_req = [s.route for s in t.rank("improvement_per_request")]
    per_sec = [s.route for s in t.rank("improvement_per_second")]
    assert per_req[0] == "slow_strong"
    assert per_sec[0] == "fast_weak"


def test_per_token_view_tracks_billing_not_wall_clock():
    t = RouteQualityTracker(min_attempts=5)
    feed(t, "cheap", 10, delta=0.1, tokens=500, latency_ms=5000)
    feed(t, "expensive", 10, delta=0.15, tokens=10_000, latency_ms=5000)
    assert t.rank("improvement_per_1k_tokens")[0].route == "cheap"


# ------------------------------------------ refusing to overclaim
def test_routes_below_the_threshold_are_excluded_not_ranked_low():
    t = RouteQualityTracker(min_attempts=20)
    feed(t, "lucky", 2, delta=10.0)          # two spectacular samples
    feed(t, "solid", 25, delta=0.1)
    ranked = [s.route for s in t.rank()]
    assert "lucky" not in ranked, "a 2-sample route must not top the table"
    assert ranked == ["solid"]
    assert "lucky" in t.compare()["excluded_insufficient_data"]


def test_verdict_refuses_to_recommend_on_one_route():
    t = RouteQualityTracker(min_attempts=5)
    feed(t, "only_one", 10, delta=0.5)
    v = t.compare()["verdict"]
    assert "insufficient evidence" in v
    assert "Do not change routing" in v


def test_verdict_refuses_to_recommend_on_a_narrow_margin():
    t = RouteQualityTracker(min_attempts=5)
    feed(t, "a", 10, delta=0.100, latency_ms=1000)
    feed(t, "b", 10, delta=0.105, latency_ms=1000)   # ~5% better
    v = t.compare()["verdict"]
    assert "too close to call" in v


def test_verdict_recommends_only_with_a_clear_margin_and_still_defers():
    t = RouteQualityTracker(min_attempts=5)
    feed(t, "winner", 10, delta=1.0, latency_ms=1000)
    feed(t, "loser", 10, delta=0.05, latency_ms=1000)
    v = t.compare()["verdict"]
    assert "winner" in v and "leads on improvement/second" in v
    # The operator chose the primary; the verdict proposes, it does not decide.
    assert "operator preference" in v


def test_verdict_handles_no_improvements_at_all():
    t = RouteQualityTracker(min_attempts=5)
    feed(t, "a", 10, delta=0.0)
    feed(t, "b", 10, delta=0.0)
    assert "no route produced a measurable improvement" in t.compare()["verdict"]


# ------------------------------------------------------------ per-operator
def test_operator_breakdown_can_differ_from_the_overall_winner():
    """
    A slow model may earn its latency on RADICAL_RETHINK and waste it on
    PARAMETER_CHANGE — which argues for routing per operator, not one winner.
    """
    t = RouteQualityTracker(min_attempts=1)
    feed(t, "slow_strong", 5, delta=0.9, operator="RADICAL_RETHINK", latency_ms=10_000)
    feed(t, "fast_weak", 5, delta=0.05, operator="RADICAL_RETHINK", latency_ms=1_000)
    feed(t, "slow_strong", 5, delta=0.02, operator="PARAMETER_CHANGE", latency_ms=10_000)
    feed(t, "fast_weak", 5, delta=0.30, operator="PARAMETER_CHANGE", latency_ms=1_000)

    b = t.operator_breakdown()
    assert b["RADICAL_RETHINK"][0]["route"] == "slow_strong"
    assert b["PARAMETER_CHANGE"][0]["route"] == "fast_weak"


# ------------------------------------------------------------ plumbing
def test_snapshot_is_serialisable():
    t = RouteQualityTracker(min_attempts=1)
    feed(t, "a", 3, delta=0.2, operator="LOCAL_OPTIMIZE")
    json.dumps(t.to_dict())


def test_render_produces_a_table():
    t = RouteQualityTracker(min_attempts=1)
    feed(t, "opencode_zen/x-preview-f-free", 5, delta=0.2)
    out = t.render()
    assert "x-preview-f-free" in out and "impr/req" in out


def test_render_is_honest_when_empty():
    assert "no mutation attempts" in RouteQualityTracker().render()


def test_round_trips_through_disk(tmp_path):
    t = RouteQualityTracker(min_attempts=1)
    feed(t, "a", 7, delta=0.3, tokens=2000)
    path = str(tmp_path / "quality.json")
    t.save(path)
    back = RouteQualityTracker.load(path)
    assert back.routes["a"].attempts == 7
    assert back.routes["a"].total_tokens == 14_000


def test_corrupt_or_missing_history_is_not_fatal(tmp_path):
    """Quality data is an optimisation input, never a correctness input."""
    assert RouteQualityTracker.load(str(tmp_path / "nope.json")).routes == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert RouteQualityTracker.load(str(bad)).routes == {}
