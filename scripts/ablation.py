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

The confound to watch
---------------------

Arms run one after another, so they do not sample the same provider. That is
not hypothetical: within a single session Ox Alpha went 40% → 11% success and
nemotron went 77% → 48% with its p50 latency doubling. An arm that ran through
the bad half looks worse for reasons that have nothing to do with the feature.

Each arm's provider success rate and mean latency are therefore printed
alongside its result, and a comparison across materially different conditions
is caveated rather than reported clean.

The structural answer is **`--repeats 3`**, and it needs no flag: repeats are
already interleaved, because the loop runs a fresh baseline before each round
of arms rather than all baselines first. So three repeats samples every arm
three times across the session, and drift lands on both sides instead of
concentrating in whichever ran second. With `--repeats 1` there is nothing to
interleave and the arms are simply sequential — which is how the first recorded
ablation was run, and why its multi-offspring latency figure carries an
ambiguity it cannot resolve.

Judge conditions by **latency, not success rate**. The broker retries, so a
run's recorded success rate reads 100% while the provider is at 48%; the cost
of those retries lands entirely in latency.

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
    "operator_bandit": {
        "env": {"OE_MAX_OPERATORS": "1", "OE_MAX_OPERATOR_BANDIT": "1"},
        "asks": "does letting measured reward pick the operator beat picking "
                "uniformly at random?",
        "note": "the bandit acts through operator steering, so this arm turns "
                "both on. Compare it against the `operators` arm, not only "
                "against the baseline, or you measure steering and selection "
                "together and cannot say which one paid.\n"
                "    Read this arm with more suspicion than the others: a "
                "12-iteration run gives the bandit roughly a dozen "
                "observations spread over fifteen arms, which is thin enough "
                "that it will mostly still be exploring. A null result here is "
                "evidence about short runs, not about the bandit.\n"
                "    It is also the one arm that is not reproducible: "
                "selection depends on rewards from earlier iterations, so a "
                "rerun with the same seed diverges as soon as a score does.",
    },
    "multi_offspring": {
        "env": {"OE_MAX_MULTI_OFFSPRING": "3"},
        "asks": "do three alternatives per request beat one, after "
                "deduplication?",
    },
    "seed_forge": {
        "env": {"OE_MAX_SEED_FORGE": "3"},
        "needs_evaluator": True,
        "asks": "does starting from a forged population beat starting from one "
                "program?",
        "note": "the variants cost evaluation time but no model requests, so "
                "read this arm on area-under-curve per *request* — measured "
                "against wall-clock it is paying for an advantage it did not "
                "earn.",
    },
    "verify": {
        "env": {"OE_MAX_VERIFY": "1"},
        "needs_evaluator": True,
        "asks": "what does verification cost, and does it change the outcome?",
        "note": "verification only observes — it never removes a candidate — "
                "so a difference in outcome here is variance, not effect. The "
                "number worth reading from this arm is the time it added.",
    },
}


def arm_env(name: str, task: str) -> Dict[str, str]:
    """
    The environment for one arm, with task-derived paths filled in.

    Derived rather than hardcoded because both `seed_forge` and `verify` need
    to know where the evaluator is, and a hardcoded path silently degrades when
    `--task` changes: seeding skips entirely, and verification quietly falls
    back to generic checks only — an arm that looks like it ran and tested
    almost nothing.
    """
    env = dict(ARMS[name]["env"])
    if ARMS[name].get("needs_evaluator"):
        env["EVOLUTION_EVALUATOR_PATH"] = os.path.join(
            "examples", task, "evaluator.py")
        # The task's entry point, not the default: function_minimization
        # evolves `search_algorithm`, and verifying `run_search` would check a
        # wrapper rather than the thing that changed.
        env.setdefault("OE_MAX_VERIFY_ENTRY_POINT",
                       os.environ.get("OE_MAX_VERIFY_ENTRY_POINT",
                                      "search_algorithm"))
    return env


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
    sys.path.insert(0, ROOT)
    from oe_max.console import use_utf8_stdio
    # Before any print: a character the console code page cannot encode
    # raises rather than mangling, and kills the process. See
    # oe_max/console.py.
    use_utf8_stdio()
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
            print(f"    env : {arm_env(name, 'function_minimization')}")
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
            results.append(_run(f"{arm}-{rep + 1}", arm_env(arm, args.task), args))
            checkpoint()

    checkpoint(complete=True)
    _report(results, arms, manifest_path)
    return 0


def _report(results: List[Dict[str, Any]], arms: List[str],
            manifest_path: str) -> None:
    sys.path.insert(0, ROOT)
    from control_plane.analysis.outcome import compare, provider_conditions
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

    # Arms run one after another, so they do not sample the same provider.
    # Printed for every arm rather than only when it drifts, because a reader
    # comparing two numbers deserves to see the conditions each was measured
    # under without being told to go looking.
    print("\n=== provider conditions, per arm ===")
    for r in results:
        c = provider_conditions(conn, r["run_id"])
        rate = f"{c['success_rate']:.0%}" if c["success_rate"] is not None else "—"
        lat = f"{c['mean_latency_s']}s" if c["mean_latency_s"] is not None else "—"
        print(f"  {r['name']:<26} success={rate:<6} mean={lat}")

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
