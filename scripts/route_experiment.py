"""
Run the same task on each of several routes and compare what they produced.

This is the experiment T1 asks for, made repeatable. The comparison it prints
is the one from `oe_max.route_quality`: not "did the route respond?" — the
broker's own stats already answer that — but "did it produce better mutations,
and at what cost?" A route can be perfectly reliable and return nothing but
duplicates, and the two questions were already measured coming apart:

    x-preview-f-free (Ox Alpha)   29% success   292 s/request
    nemotron-3-ultra-free        100% success   112 s/request

Design notes that are not incidental:

* **One base config, pinned per arm.** The arms differ in exactly one field —
  `llm.primary_model` — because a temporary config is derived from the base
  rather than kept as a second checked-in file that can drift. Naming a
  concrete model makes the broker pin that route instead of running the
  failover chain, which is what makes an arm an arm.

* **The same seed for every arm.** Otherwise a difference in the search
  trajectory is indistinguishable from a difference in the model.

* **Runs are pooled per route, not averaged per run.** One short run rarely
  clears `MIN_ATTEMPTS_FOR_COMPARISON` on any route; pooling comparable runs
  is the honest way to reach it, and every run that went in is named in the
  output so a reader can check they were comparable.

* **It refuses to declare a winner on thin evidence.** The verdict comes from
  `RouteQualityTracker.compare`, which says so explicitly rather than ranking
  noise. The point of the experiment is to be able to act on the result; a
  confident answer from four samples is worse than no answer.

Usage:

    scripts/route_experiment.py --routes x-preview-f-free,nemotron-3-ultra-free \\
                               --iterations 12 --repeats 2
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
BASE_CONFIG = os.path.join("configs", "oe_max", "evolution.yaml")
API = os.environ.get("EVOLUTION_API", "http://127.0.0.1:8000")
BROKER = os.environ.get("OE_MAX_URL", "http://127.0.0.1:8787")


def _get(url: str, timeout: float = 30.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(url: str, body: Dict[str, Any], timeout: float = 60.0) -> Any:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _preflight(routes: List[str]) -> None:
    """
    Fail before spending an hour, not during it.

    A route that the broker will not serve produces an arm of pure failures,
    which is data of a sort but not the data this was run for.
    """
    try:
        health = _get(f"{BROKER}/health", timeout=10.0)
    except (urllib.error.URLError, OSError) as e:
        sys.exit(f"broker unreachable at {BROKER}: {e}\n"
                 f"start it first:  ./scripts/start-broker.sh")
    if health.get("status") != "ok":
        sys.exit(f"broker is not healthy: {health.get('status')}")

    try:
        _get(f"{API}/api/health", timeout=10.0)
    except (urllib.error.URLError, OSError) as e:
        sys.exit(f"control plane unreachable at {API}: {e}\nstart it first:  ./run.sh")

    eligible = set(_get(f"{BROKER}/v1/oe-max/status", timeout=15.0)
                   ["router"].get("eligible", []))
    known = {r.split("/", 1)[-1] for r in eligible}
    unknown = [r for r in routes if r not in known]
    if unknown:
        sys.exit(f"not currently eligible on the broker: {', '.join(unknown)}\n"
                 f"eligible now: {', '.join(sorted(known)) or '(none)'}\n"
                 f"run ./scripts/verify-providers.sh to re-probe")


def _pinned_config(base_config: str, route: str, seed: int, out_dir: str) -> str:
    """
    Derive a config that pins one route, from the single base config.

    A checked-in file per arm would drift from the base the first time anyone
    tuned max_tokens; deriving it means an arm can only ever differ in the
    field this experiment is varying.
    """
    import yaml

    with open(os.path.join(ROOT, base_config), "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh)
    cfg.setdefault("llm", {})["primary_model"] = route
    cfg["random_seed"] = seed
    path = os.path.join(out_dir, f"pinned-{route.replace('/', '_')}.yaml")
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(cfg, fh, sort_keys=False)
    return path


def _wait(run_id: str, poll_s: float, timeout_s: float) -> str:
    """Poll until the run leaves `running`; return its final status."""
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        try:
            # The control API creates the run; the query projection only knows
            # about it once its first events are ingested, so a freshly started
            # run reads as "not found" for a moment. Treating that as "starting"
            # rather than an error is the difference between a working poll and
            # one that dies on the first tick.
            status = _get(f"{API}/api/query/runs/{run_id}").get("status") or "starting"
        except (urllib.error.URLError, OSError, KeyError, TypeError):
            status = last or "starting"
        if status != last:
            print(f"    {run_id[:16]}… {status}", flush=True)
            last = status
        if status not in ("running", "created", "starting"):
            return status
        time.sleep(poll_s)

    print(f"    {run_id[:16]}… timed out after {timeout_s:.0f}s — stopping it",
          flush=True)
    try:
        _post(f"{API}/api/control/runs/{run_id}/stop", {})
    except (urllib.error.URLError, OSError):
        pass
    # Reported as a timeout rather than as a completed arm: its attempts are
    # real and still count, but the arm did not run to the same length as the
    # others and the summary has to say so.
    return "timeout"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--routes", required=True,
                    help="comma-separated model ids to pin, one arm each")
    ap.add_argument("--task", default="function_minimization")
    ap.add_argument("--iterations", type=int, default=12)
    ap.add_argument("--repeats", type=int, default=1,
                    help="runs per route; pooled per route in the comparison")
    ap.add_argument("--seed", type=int, default=42,
                    help="same seed for every arm, so the arms differ only in route")
    ap.add_argument("--min-attempts", type=int, default=None,
                    help="override the minimum attempts a route needs to be ranked")
    ap.add_argument("--timeout", type=float, default=5400.0,
                    help="seconds to wait for one run before stopping it")
    ap.add_argument("--config", default=BASE_CONFIG)
    ap.add_argument("--json", dest="as_json", action="store_true",
                    help="print the full comparison as JSON")
    args = ap.parse_args()

    routes = [r.strip() for r in args.routes.split(",") if r.strip()]
    if len(routes) < 2:
        print("note: one route only — this will measure it, not compare it.\n",
              file=sys.stderr)
    _preflight(routes)

    program = os.path.join("examples", args.task, "initial_program.py")
    evaluator = os.path.join("examples", args.task, "evaluator.py")
    for f in (program, evaluator):
        if not os.path.exists(os.path.join(ROOT, f)):
            sys.exit(f"missing: {f}")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out_dir = os.path.join(ROOT, "runs", f"route-experiment-{stamp}")
    os.makedirs(out_dir, exist_ok=True)

    manifest_path = os.path.join(out_dir, "experiment.json")
    run_ids: List[str] = []
    results: List[Dict[str, Any]] = []

    def checkpoint() -> None:
        """
        Write what is known after every arm.

        Learned the hard way: an experiment that only writes its manifest at
        the end loses every completed arm when the machine goes away mid-run,
        and these arms cost tens of minutes each. The run ids are the valuable
        part — the analysis can be recomputed from the store at any time.
        """
        with open(manifest_path, "w", encoding="utf-8") as fh:
            json.dump({"routes": routes, "task": args.task,
                       "iterations": args.iterations, "repeats": args.repeats,
                       "seed": args.seed, "arms": results, "run_ids": run_ids,
                       "complete": len(results) == len(routes) * args.repeats},
                      fh, indent=2)

    for route in routes:
        cfg_path = _pinned_config(args.config, route, args.seed, out_dir)
        for rep in range(args.repeats):
            name = f"route-{route}-{rep + 1}"
            print(f"\n=== {name} ({args.iterations} iterations) ===", flush=True)
            started = time.time()
            run = _post(f"{API}/api/control/runs", {
                "initial_program": program,
                "evaluator": evaluator,
                "config_path": os.path.relpath(cfg_path, ROOT),
                "iterations": args.iterations,
                "name": name,
            })
            run_id = run["run_id"]
            run_ids.append(run_id)
            status = _wait(run_id, poll_s=10.0, timeout_s=args.timeout)
            results.append({"route": route, "run_id": run_id, "status": status,
                            "wall_clock_s": round(time.time() - started, 1)})
            checkpoint()

    print("\n=== arms ===")
    for r in results:
        print(f"  {r['route']:<32} {r['status']:<10} {r['wall_clock_s']:>8.0f}s  {r['run_id']}")

    query = f"{API}/api/query/runs/{run_ids[0]}/route-quality"
    params = []
    if len(run_ids) > 1:
        params.append("pool=" + ",".join(run_ids[1:]))
    if args.min_attempts is not None:
        params.append(f"min_attempts={args.min_attempts}")
    if params:
        query += "?" + "&".join(params)
    comparison = _get(query, timeout=120.0)

    manifest = {"routes": routes, "task": args.task, "iterations": args.iterations,
                "repeats": args.repeats, "seed": args.seed, "arms": results,
                "run_ids": run_ids, "complete": True, "comparison": comparison}
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    if args.as_json:
        print(json.dumps(manifest, indent=2))
    else:
        _print_comparison(comparison)
    print(f"\nfull result: {manifest_path}")
    return 0


def _print_comparison(comparison: Dict[str, Any]) -> None:
    print("\n=== attribution coverage ===")
    for cov in comparison.get("coverage", []):
        note = f"  — {cov['note']}" if cov.get("note") else ""
        print(f"  {cov['run_id'][:20]:<22} candidates={cov['candidates']:<4} "
              f"attributed={cov['attributed']:<4} requests={cov['mutation_requests']}{note}")

    routes = comparison.get("routes", {})
    if not routes:
        print("\nno mutation attempts were recorded — nothing to compare.")
        return

    print("\n=== per route ===")
    header = ("{:<30}{:>5}{:>6}{:>7}{:>6}{:>8}{:>9}{:>10}{:>11}"
              .format("route", "n", "fail", "unparse", "dup", "valid", "improv",
                      "mean s", "impr/req"))
    print(header)
    print("-" * len(header))
    for s in sorted(routes.values(), key=lambda d: d["attempts"], reverse=True):
        print("{:<30}{:>5}{:>6}{:>7}{:>6}{:>8.0%}{:>9.0%}{:>10}{:>11}".format(
            s["route"][:29], s["attempts"], s["failures"], s["unparseable"],
            s["duplicates"], s["validity_rate"], s["improvement_rate"],
            f"{s['mean_latency_s']:.0f}" if s.get("mean_latency_s") else "-",
            f"{s['improvement_per_request']:.4f}"))

    comp = comparison.get("comparison", {})
    for measure, rows in (comp.get("views") or {}).items():
        if rows:
            print(f"\n  best by {measure}: " +
                  ", ".join(r["route"] for r in rows[:3]))
    for route, why in (comp.get("excluded_insufficient_data") or {}).items():
        print(f"  excluded: {route} — {why}")

    print("\n=== verdict ===")
    print("  " + (comp.get("verdict") or "no verdict"))


if __name__ == "__main__":
    sys.exit(main())
