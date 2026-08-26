"""
Ablations: does each feature actually help?

Every optional behaviour in OE-MAX is off by default and gated by an
environment variable, which was a deliberate choice with a cost attached — a
feature nobody can turn off is a feature nobody can measure, but a feature
nobody *has* measured is not an improvement either, it is a hypothesis with a
flag. This runs the same task with each one on and off and reports the
difference.

The measure is area under the best-so-far curve against **requests**, not
wall-clock and not final score:

* against requests, because the objective divides by requests and a request is
  what costs money and rate budget. Against wall-clock a fast model wins by
  being fast, which the latency numbers already say.
* area rather than final score, because a final score is close to a single
  sample — it is whatever the last lucky draw produced. Area rewards finding a
  good program early and keeping it, which is what a search is for.

Both are reported anyway, and when they disagree the verdict says so: an arm
that ends higher but climbs later is a real trade-off, not a tie.

Structure: one shared baseline per repeat, then each arm, all on the same seed.
A per-arm baseline would double the cost and add a second source of variance
between the things being compared.

    scripts/ablation.py --arms operators,island_policies --repeats 2

Run it against a real provider
------------------------------

The local test provider (`./run.sh provider`) replays a fixed pool of five
diffs and does not read the prompt, so the `operators` and `island_policies`
arms **cannot** show an effect against it — whatever the prompt asks for, the
same five mutations come back. A difference there is variance, and a smoke test
of this harness produced exactly that: the `operators` arm looked 1.75x better
on distinct yield purely by luck over eight requests.

The one arm the stub can answer is `multi_offspring`, because the provider does
honour a request for N alternatives. Everything else needs the broker.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.environ.get("EVOLUTION_API", "http://127.0.0.1:8000")

# Each arm is the environment that turns one behaviour on. The baseline is the
# absence of all of them, which is also what a plain upstream run does.
ARMS: Dict[str, Dict[str, Any]] = {
    "operators": {
        "env": {"OE_MAX_OPERATORS": "1"},
        "asks": "does naming the mutation class beat one undifferentiated "
                "'improve this program' request?",
    },
    "island_policies": {
        "env": {"OE_MAX_OPERATORS": "1", "OE_MAX_ISLAND_POLICIES": "1"},
        "asks": "does giving each island a different search posture beat "
                "running the same search on all of them?",
        "note": "island policies act through operator steering, so this arm "
                "turns both on. Compare it against the `operators` arm, not "
                "only against the baseline, to isolate the policy layer.",
    },
    "multi_offspring": {
        "env": {"OE_MAX_MULTI_OFFSPRING": "3"},
        "asks": "do three alternatives per request beat one, after "
                "deduplication?",
    },
    "verify": {
        "env": {"OE_MAX_VERIFY": "1"},
        "asks": "what does verification cost, and does it change the outcome?",
        "note": "verification only observes — it never removes a candidate — "
                "so a difference in outcome here is variance, not effect. The "
                "number worth reading from this arm is the time it added.",
    },
}


def _get(url: str, timeout: float = 30.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(url: str, body: Dict[str, Any], timeout: float = 60.0) -> Any:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _wait(run_id: str, timeout_s: float, poll_s: float = 10.0) -> str:
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        try:
            status = _get(f"{API}/api/query/runs/{run_id}").get("status") or "starting"
        except (urllib.error.URLError, OSError, KeyError, TypeError):
            status = last or "starting"
        if status != last:
            print(f"    {run_id[:16]}… {status}", flush=True)
            last = status
        if status not in ("running", "created", "starting"):
            return status
        time.sleep(poll_s)
    try:
        _post(f"{API}/api/control/runs/{run_id}/stop", {})
    except (urllib.error.URLError, OSError):
        pass
    return "timeout"


def _run(name: str, env: Dict[str, str], args) -> Dict[str, Any]:
    print(f"\n=== {name} ===", flush=True)
    started = time.time()
    run = _post(f"{API}/api/control/runs", {
        "initial_program": os.path.join("examples", args.task, "initial_program.py"),
        "evaluator": os.path.join("examples", args.task, "evaluator.py"),
        "config_path": args.config,
        "iterations": args.iterations,
        "name": name,
        "env": env,
    })
    status = _wait(run["run_id"], args.timeout)
    return {"name": name, "run_id": run["run_id"], "status": status,
            "wall_clock_s": round(time.time() - started, 1), "env": env}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--arms", default=",".join(ARMS),
                    help=f"comma-separated: {', '.join(ARMS)}")
    ap.add_argument("--task", default="function_minimization")
    ap.add_argument("--config", default="configs/evolution/local_test.yaml")
    ap.add_argument("--iterations", type=int, default=12)
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--timeout", type=float, default=3600.0)
    ap.add_argument("--list", action="store_true", help="show the arms and exit")
    args = ap.parse_args()

    if args.list:
        for name, arm in ARMS.items():
            print(f"{name}\n    asks: {arm['asks']}")
            if arm.get("note"):
                print(f"    note: {arm['note']}")
            print(f"    env : {arm['env']}")
        return 0

    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    unknown = [a for a in arms if a not in ARMS]
    if unknown:
        sys.exit(f"unknown arm(s): {', '.join(unknown)}. Known: {', '.join(ARMS)}")

    try:
        _get(f"{API}/api/health", timeout=10.0)
    except (urllib.error.URLError, OSError) as e:
        sys.exit(f"control plane unreachable at {API}: {e}\nstart it with ./run.sh")

    out_dir = os.path.join(ROOT, "runs",
                           f"ablation-{time.strftime('%Y%m%d-%H%M%S')}")
    os.makedirs(out_dir, exist_ok=True)
    manifest_path = os.path.join(out_dir, "ablation.json")

    results: List[Dict[str, Any]] = []

    def checkpoint(complete: bool = False) -> None:
        # Written after every run: these arms cost tens of minutes each, and an
        # interrupted experiment should lose one of them rather than all.
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump({"task": args.task, "iterations": args.iterations,
                       "repeats": args.repeats, "arms": arms,
                       "runs": results, "complete": complete}, fh, indent=2)

    for rep in range(args.repeats):
        results.append(_run(f"baseline-{rep + 1}", {}, args))
        checkpoint()
        for arm in arms:
            results.append(_run(f"{arm}-{rep + 1}", dict(ARMS[arm]["env"]), args))
            checkpoint()

    checkpoint(complete=True)
    _report(results, arms, manifest_path)
    return 0


def _report(results: List[Dict[str, Any]], arms: List[str],
            manifest_path: str) -> None:
    sys.path.insert(0, ROOT)
    from control_plane.analysis.outcome import compare
    from control_plane.analysis.throughput import measure
    from control_plane.storage.store import Store

    workspace = _get(f"{API}/api/health")["workspace"]
    store = Store(os.path.join(workspace, "control_plane.db"))
    conn = store.reader()

    baseline_ids = [r["run_id"] for r in results if r["name"].startswith("baseline")]

    print("\n=== runs ===")
    for r in results:
        print(f"  {r['name']:<26} {r['status']:<10} {r['wall_clock_s']:>8.0f}s  "
              f"{r['run_id'][:16]}…")

    print("\n=== yield per request ===")
    for r in results:
        m = measure(conn, r["run_id"])
        print(f"  {r['name']:<26} req={m['mutation_requests']:<4} "
              f"raw={m['candidates_per_request']}  "
              f"distinct={m['useful_candidates_per_request']}  "
              f"dup={m['duplicate_share']}")

    print("\n=== outcome, against the baseline ===")
    for arm in arms:
        arm_ids = [r["run_id"] for r in results if r["name"].startswith(f"{arm}-")]
        result = compare(conn, baseline_ids, arm_ids,
                         baseline_name="baseline", treatment_name=arm)
        print(f"\n  {arm}")
        print(f"    asks: {ARMS[arm]['asks']}")
        if ARMS[arm].get("note"):
            print(f"    note: {ARMS[arm]['note']}")
        print(f"    {result['verdict']}")

    store.close()
    print(f"\nfull result: {manifest_path}")


if __name__ == "__main__":
    sys.exit(main())
