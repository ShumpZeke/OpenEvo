"""
Build a per-route mutation-quality picture from a run's stored telemetry.

`oe_max.route_quality` defines *what* the measures mean. This module answers
the only question that made them useful: where do the numbers come from in a
real run? It reads the projections the telemetry pipeline already maintains and
turns them into `Attempt` records — no new instrumentation, no second source of
truth, and nothing that could disagree with what the Control Center shows.

The attempt set is the set of **mutation-role model requests**, not the set of
candidates, and the difference is the whole point. A route that burns 229
seconds and returns an unusable diff produced no candidate at all; counting
only candidates would make that route look free. Every request is charged:

    request failed          the route cost time and returned nothing
    no candidate, no reject the response contained no applicable diff
    rejected                a duplicate, or it lost the novelty gate
    candidate stored        it entered the population; delta vs its parent

This depends on candidate→request attribution being present. Runs recorded
before that existed have `gen_request_id IS NULL` on every candidate and will
report zero attempts rather than a plausible-looking guess — see
`attribution_coverage`.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

from oe_max.route_quality import Attempt, RouteQualityTracker

# What the instrumentation labels a request that generated a mutation. An
# evaluator-feedback call is labelled "evaluation" and is deliberately excluded:
# it grades a candidate rather than proposing one.
MUTATION_ROLE = "mutation"

_ATTEMPT_SQL = """
SELECT m.request_id,
       m.provider,
       m.model,
       m.status,
       m.latency_ms,
       m.total_tokens,
       m.metadata            AS request_metadata,
       c.candidate_id,
       c.gen_operator        AS operator,
       c.combined_score      AS child_score,
       p.combined_score      AS parent_score
  FROM model_requests m
  LEFT JOIN candidates c
         ON c.run_id = m.run_id AND c.gen_request_id = m.request_id
  LEFT JOIN candidates p
         ON p.run_id = c.run_id AND p.candidate_id = c.parent_id
 WHERE m.run_id = ?
   AND (m.role = ? OR m.role IS NULL)
 ORDER BY m.started_at
"""

# Rejections are events, not rows: a rejected candidate never enters the
# `candidates` projection, so the only record that the route spent an attempt
# is the event it emitted.
_REJECTION_SQL = """
SELECT payload FROM events
 WHERE run_id = ? AND type = 'candidate.rejected'
"""


def _reasoning_tokens(request_metadata: Optional[str]) -> int:
    try:
        md = json.loads(request_metadata or "{}")
    except ValueError:
        return 0
    for key in ("reasoning_tokens", "completion_reasoning_tokens"):
        v = md.get(key)
        if isinstance(v, (int, float)):
            return int(v)
    return 0


def _rejected_request_ids(conn: sqlite3.Connection, run_id: str) -> Dict[str, int]:
    """request_id → how many candidates it produced that were rejected."""
    counts: Dict[str, int] = {}
    for (payload,) in conn.execute(_REJECTION_SQL, (run_id,)):
        try:
            md = (json.loads(payload) or {}).get("metadata") or {}
        except ValueError:
            continue
        rid = md.get("generating_request_id")
        if rid:
            counts[rid] = counts.get(rid, 0) + 1
    return counts


def _rejected_operators(conn: sqlite3.Connection, run_id: str) -> Dict[str, str]:
    """
    request_id → the operator that was asked for.

    A rejected candidate never reaches the `candidates` projection, so its
    operator is only in the event. Without this, every duplicate would be
    unlabelled and the per-operator duplicate rate — the number that says an
    operator is producing nothing new — would be blank for exactly the
    operators worth catching.
    """
    out: Dict[str, str] = {}
    for (payload,) in conn.execute(_REJECTION_SQL, (run_id,)):
        try:
            md = (json.loads(payload) or {}).get("metadata") or {}
        except ValueError:
            continue
        rid, op = md.get("generating_request_id"), md.get("generating_operator")
        if rid and op:
            out[rid] = op
    return out


def build_tracker(conn: sqlite3.Connection, run_id: str,
                  min_attempts: Optional[int] = None) -> RouteQualityTracker:
    """Replay a run's stored requests into a `RouteQualityTracker`."""
    tracker = (RouteQualityTracker(min_attempts=min_attempts)
               if min_attempts is not None else RouteQualityTracker())
    rejected = _rejected_request_ids(conn, run_id)
    rejected_operators = _rejected_operators(conn, run_id)

    conn.row_factory = sqlite3.Row
    for row in conn.execute(_ATTEMPT_SQL, (run_id, MUTATION_ROLE)):
        provider = row["provider"] or "unknown"
        model = row["model"] or "unknown"
        failed = (row["status"] or "").lower() not in ("ok", "succeeded", "completed", "")
        was_rejected = row["request_id"] in rejected
        accepted = row["candidate_id"] is not None

        # A request that returned, produced no stored candidate and drew no
        # rejection event yielded nothing applicable. That is the "no valid
        # diffs found in response" case, and the route is charged for it.
        parsed = failed or accepted or was_rejected

        delta: Optional[float] = None
        if accepted and row["child_score"] is not None and row["parent_score"] is not None:
            delta = float(row["child_score"]) - float(row["parent_score"])

        tracker.record(Attempt(
            route=f"{provider}/{model}",
            # Unlabelled unless operator steering was on for the run; upstream
            # issues one undifferentiated request and there is no operator to
            # recover after the fact.
            operator=row["operator"] or rejected_operators.get(row["request_id"]),
            failed=failed,
            parsed=parsed,
            passed_g0=not failed and parsed,
            passed_g1=not was_rejected,
            accepted=accepted,
            fitness_delta=delta,
            latency_ms=float(row["latency_ms"] or 0.0),
            tokens=int(row["total_tokens"] or 0),
            reasoning_tokens=_reasoning_tokens(row["request_metadata"]),
        ))
    return tracker


def attribution_coverage(conn: sqlite3.Connection, run_id: str) -> Dict[str, Any]:
    """
    How much of this run can actually be analysed.

    Reported alongside every result rather than left implicit: a comparison
    drawn from 3 of 40 candidates is not a weaker version of the same answer,
    it is a different claim, and the caller has to be able to see which one
    they are being given.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "       SUM(CASE WHEN gen_request_id IS NOT NULL THEN 1 ELSE 0 END) AS attributed "
        "  FROM candidates WHERE run_id = ?", (run_id,)).fetchone()
    total = int(row[0] or 0)
    attributed = int(row[1] or 0)
    requests = conn.execute(
        "SELECT COUNT(*) FROM model_requests WHERE run_id = ? AND (role = ? OR role IS NULL)",
        (run_id, MUTATION_ROLE)).fetchone()[0]

    note = None
    if total and not attributed:
        note = ("no candidate in this run carries generation provenance — it "
                "predates attribution, so no route comparison is possible")
    elif total and attributed < total:
        # Expected, not a defect: the seed program had no generating request
        # and migrants are copies of an already-charged generation.
        note = (f"{total - attributed} of {total} candidates are unattributed "
                f"(seed program and migrant copies are unattributable by design)")

    return {
        "candidates": total,
        "attributed": attributed,
        "mutation_requests": int(requests or 0),
        "note": note,
    }


def analyse(conn: sqlite3.Connection, run_id: str,
            min_attempts: Optional[int] = None) -> Dict[str, Any]:
    """The full answer for one run: coverage first, then the comparison."""
    from .throughput import measure

    tracker = build_tracker(conn, run_id, min_attempts=min_attempts)
    return {
        "run_id": run_id,
        "coverage": attribution_coverage(conn, run_id),
        # Yield per request, reported alongside per-route quality because the
        # two are usually read together: a route that answers twice as fast is
        # worth nothing if its extra output is duplicate code.
        "throughput": measure(conn, run_id),
        **tracker.to_dict(),
    }


def analyse_runs(conn: sqlite3.Connection, run_ids: List[str],
                 min_attempts: Optional[int] = None) -> Dict[str, Any]:
    """
    Pool several runs into one comparison.

    A single short run rarely clears `MIN_ATTEMPTS_FOR_COMPARISON` on any
    route, and pooling is the honest way to get there — as long as the runs
    are named, so a reader can check they were comparable.
    """
    tracker = (RouteQualityTracker(min_attempts=min_attempts)
               if min_attempts is not None else RouteQualityTracker())
    coverage = []
    for run_id in run_ids:
        part = build_tracker(conn, run_id, min_attempts=min_attempts)
        for route, stats in part.routes.items():
            target = tracker.routes.setdefault(route, type(stats)(route))
            for field, value in stats.__dict__.items():
                if field == "route":
                    continue
                if field == "best_delta":
                    if value is not None:
                        target.best_delta = (value if target.best_delta is None
                                             else max(target.best_delta, value))
                elif isinstance(value, (int, float)):
                    setattr(target, field, getattr(target, field) + value)
        coverage.append({"run_id": run_id, **attribution_coverage(conn, run_id)})
    return {"runs": run_ids, "coverage": coverage, **tracker.to_dict()}
