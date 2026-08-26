"""
Did the run actually get anywhere?

Route quality answers "which route produced better mutations" and throughput
answers "how many candidates per request". Neither answers the question an
ablation actually asks: **did this configuration find a better program, for the
same number of requests?**

Best-so-far against *requests*, not wall-clock
----------------------------------------------

Plotting against wall-clock makes a fast model win by being fast, which is a
different claim and one the latency numbers already make. The objective divides
by requests, and a request is the unit that costs money and rate budget, so the
x-axis is requests.

Area under the best-so-far curve, not the final score
-----------------------------------------------------

Final score alone is close to a single sample: it is whatever the last lucky
draw produced, and it says nothing about whether the run got there on request 3
or request 30. Area under the curve rewards finding a good program *early* and
holding it, which is what a search is for. Both are reported, because a
configuration that ends higher but climbs later is a real trade-off rather than
a tie.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional

MUTATION_ROLE = "mutation"


def best_so_far(conn: sqlite3.Connection, run_id: str) -> List[Dict[str, Any]]:
    """
    The champion's score after each generated candidate, in order.

    Migrants are excluded: a migrant is a copy of a program already counted, so
    including them would make a run look like it kept improving while it was
    only copying itself between islands.
    """
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT candidate_id, combined_score, created_at FROM candidates "
        " WHERE run_id = ? AND combined_score IS NOT NULL "
        "   AND COALESCE(json_extract(metadata, '$.migrant'), 0) != 1 "
        " ORDER BY created_at, rowid", (run_id,)).fetchall()

    curve: List[Dict[str, Any]] = []
    best: Optional[float] = None
    for index, row in enumerate(rows, start=1):
        score = float(row["combined_score"])
        best = score if best is None else max(best, score)
        curve.append({"n": index, "candidate_id": row["candidate_id"],
                      "score": round(score, 6), "best": round(best, 6)})
    return curve


def area_under(curve: List[Dict[str, Any]], *, normalise: bool = True) -> Optional[float]:
    """
    Mean of the best-so-far curve.

    Normalised by length by default, because arms rarely produce the same
    number of candidates — a raw sum would reward the arm that simply ran
    longer, which is the mistake this whole module exists to avoid.
    """
    if not curve:
        return None
    total = sum(point["best"] for point in curve)
    return round(total / len(curve), 6) if normalise else round(total, 6)


def measure(conn: sqlite3.Connection, run_id: str) -> Dict[str, Any]:
    """The outcome of one run, per request."""
    curve = best_so_far(conn, run_id)
    requests = conn.execute(
        "SELECT COUNT(*) FROM model_requests WHERE run_id = ? "
        "  AND (role = ? OR role IS NULL)", (run_id, MUTATION_ROLE)).fetchone()[0]
    final = curve[-1]["best"] if curve else None

    # Where the champion stopped improving: a run that peaked at candidate 3 of
    # 30 spent 90% of its budget confirming it had already finished.
    plateau_at = None
    for point in curve:
        if final is not None and point["best"] >= final:
            plateau_at = point["n"]
            break

    return {
        "run_id": run_id,
        "mutation_requests": int(requests or 0),
        "candidates_scored": len(curve),
        "final_best": final,
        "auc_per_candidate": area_under(curve),
        "reached_best_at": plateau_at,
        "best_per_request": (round(final / requests, 6)
                             if final is not None and requests else None),
    }


def provider_conditions(conn: sqlite3.Connection, run_id: str) -> Dict[str, Any]:
    """
    What the provider was doing *while this run ran*.

    Arms of an ablation run one after another, so they are not sampling the
    same provider. That is not hypothetical: within one session Ox Alpha went
    40% → 11% success and nemotron went 77% → 48% with its p50 latency doubling.
    An arm that ran through the bad half looks worse for reasons that have
    nothing to do with the feature under test.

    Computed per run from its own requests, so it is exact rather than a
    snapshot of whatever the broker happens to report now.
    """
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT COUNT(*) AS n, "
        "       SUM(CASE WHEN status = 'ok' THEN 1 ELSE 0 END) AS ok, "
        "       AVG(latency_ms) AS mean_latency "
        "  FROM model_requests WHERE run_id = ? AND (role = ? OR role IS NULL)",
        (run_id, MUTATION_ROLE)).fetchone()
    n = int(row["n"] or 0)
    return {
        "requests": n,
        "success_rate": round(int(row["ok"] or 0) / n, 4) if n else None,
        "mean_latency_s": round((row["mean_latency"] or 0) / 1000.0, 1) if n else None,
    }


# How different two arms' provider success rates may be before the comparison
# between them is worth more caveat than conclusion.
DRIFT_TOLERANCE = 0.15

# And how far their mean latencies may diverge. This is the signal that
# actually fires, and the reason is worth knowing: the broker retries, so the
# engine records a *success* for a request the provider failed several times
# first. Run-level success rate therefore reads 100% while the broker's own
# health shows 48% — measured, on the ablation that motivated this. The cost of
# those retries lands entirely in latency, which is why latency is the drift
# signal that survives the retry layer.
LATENCY_DRIFT_RATIO = 1.5


def drift_warning(a: Dict[str, Any], b: Dict[str, Any],
                  a_name: str, b_name: str) -> Optional[str]:
    """A sentence when the two arms did not face the same provider."""
    ra, rb = a.get("success_rate"), b.get("success_rate")
    if ra is not None and rb is not None and abs(ra - rb) >= DRIFT_TOLERANCE:
        worse, better = ((b_name, a_name) if rb < ra else (a_name, b_name))
        return (f"the provider was not the same for both arms: {a_name} saw "
                f"{ra:.0%} success and {b_name} saw {rb:.0%}. {worse} ran "
                f"through worse conditions than {better}, so some of this "
                f"difference is the provider, not the feature.")

    la, lb = a.get("mean_latency_s"), b.get("mean_latency_s")
    if la and lb:
        ratio = max(la, lb) / min(la, lb)
        if ratio >= LATENCY_DRIFT_RATIO:
            slower = b_name if lb > la else a_name
            return (f"the arms did not face the same provider: mean request "
                    f"latency was {la:.0f}s for {a_name} and {lb:.0f}s for "
                    f"{b_name} ({ratio:.1f}x). {slower} was slower — either the "
                    f"provider drifted between arms, or the feature itself "
                    f"makes requests more expensive, and this number cannot "
                    f"tell you which.")
    return None


# Below this many requests per arm the comparison is noise with a decimal point.
MIN_REQUESTS_PER_ARM = 10

# A difference smaller than this is not worth acting on at these sample sizes.
MEANINGFUL_RATIO = 1.05


def compare(conn: sqlite3.Connection, baseline: List[str],
            treatment: List[str], *, baseline_name: str = "baseline",
            treatment_name: str = "treatment") -> Dict[str, Any]:
    """
    Compare two sets of runs, and say plainly when the answer is "cannot tell".

    Arms are pooled by averaging their per-run outcomes rather than by
    concatenating their candidates: two runs are two samples of the same
    configuration, and gluing their curves together would invent a single run
    that never happened.
    """
    def pooled(run_ids: List[str]) -> Dict[str, Any]:
        parts = [measure(conn, r) for r in run_ids]
        scored = [p for p in parts if p["final_best"] is not None]
        requests = sum(p["mutation_requests"] for p in parts)
        return {
            "runs": run_ids,
            "mutation_requests": requests,
            "final_best": (round(sum(p["final_best"] for p in scored) / len(scored), 6)
                           if scored else None),
            "auc_per_candidate": (
                round(sum(p["auc_per_candidate"] for p in scored) / len(scored), 6)
                if scored else None),
            "runs_scored": len(scored),
        }

    a, b = pooled(baseline), pooled(treatment)
    conditions_a = _pooled_conditions(conn, baseline)
    conditions_b = _pooled_conditions(conn, treatment)
    drift = drift_warning(conditions_a, conditions_b, baseline_name, treatment_name)

    verdict = _verdict(a, b, baseline_name, treatment_name)
    if drift:
        verdict = f"{verdict} CAVEAT: {drift}"
    return {"baseline": {"name": baseline_name, **a, "conditions": conditions_a},
            "treatment": {"name": treatment_name, **b, "conditions": conditions_b},
            "drift": drift,
            "verdict": verdict}


def _pooled_conditions(conn: sqlite3.Connection,
                       run_ids: List[str]) -> Dict[str, Any]:
    parts = [provider_conditions(conn, r) for r in run_ids]
    scored = [p for p in parts if p["success_rate"] is not None]
    requests = sum(p["requests"] for p in parts)
    if not scored:
        return {"requests": requests, "success_rate": None, "mean_latency_s": None}
    return {
        "requests": requests,
        "success_rate": round(sum(p["success_rate"] for p in scored) / len(scored), 4),
        "mean_latency_s": round(
            sum(p["mean_latency_s"] for p in scored) / len(scored), 1),
    }


def _verdict(a: Dict[str, Any], b: Dict[str, Any],
             a_name: str, b_name: str) -> str:
    if min(a["mutation_requests"], b["mutation_requests"]) < MIN_REQUESTS_PER_ARM:
        return (f"insufficient evidence: {a['mutation_requests']} and "
                f"{b['mutation_requests']} mutation requests "
                f"(need {MIN_REQUESTS_PER_ARM} per arm).")
    if a["auc_per_candidate"] is None or b["auc_per_candidate"] is None:
        return "one arm produced no scored candidates; nothing to compare."

    auc_ratio = b["auc_per_candidate"] / a["auc_per_candidate"] if a["auc_per_candidate"] else 0
    final_ratio = (b["final_best"] / a["final_best"]
                   if a["final_best"] else 0)

    if abs(auc_ratio - 1.0) < (MEANINGFUL_RATIO - 1.0):
        return (f"no measurable difference: {b_name} area-under-curve is "
                f"{auc_ratio:.3f}x {a_name}'s. At these sample sizes that is a tie.")
    direction = "better" if auc_ratio > 1 else "worse"
    caveat = ""
    if (auc_ratio > 1) != (final_ratio > 1):
        # The interesting disagreement: one arm climbs faster, the other ends
        # higher. Reporting only the winner of one would hide a real trade-off.
        caveat = (f" Note the disagreement: final best went {final_ratio:.3f}x "
                  f"the other way, so one arm climbs faster and the other ends "
                  f"higher.")
    return (f"{b_name} is {direction}: area-under-curve {auc_ratio:.3f}x, final "
            f"best {final_ratio:.3f}x, over {b['runs_scored']} and "
            f"{a['runs_scored']} scored runs.{caveat}")
