"""
Terminal-first dashboard.

The spec asks for a terminal dashboard showing experiment state, champion,
archives, operator and provider statistics, and — specifically — the NIM
rolling-window count, because that is the number an operator needs when a run
is being shaped by a rate contract.

Terminal-first rather than web-first because this is the view you want over SSH
on a long run, and because it must work when the web UI is not built. It reads
the same broker and control-plane endpoints the web UI does; there is no
separate data path and nothing is computed twice.

Renders with ANSI escapes only — no curses, no extra dependency, works in a
plain pipe (in which case it prints one frame and exits).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
CYAN = "\033[36m"
GREY = "\033[90m"


def _get(url: str, timeout: float = 5.0) -> Optional[Dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        # A dashboard must never crash the thing it is watching, and an
        # unreachable service is information rather than an error.
        return None


def _w() -> int:
    return max(60, shutil.get_terminal_size((100, 40)).columns)


def _rule(title: str = "") -> str:
    w = _w()
    if not title:
        return GREY + "─" * w + RESET
    pad = max(0, w - len(title) - 3)
    return f"{GREY}─ {BOLD}{title}{RESET}{GREY} " + "─" * pad + RESET


def _bar(value: float, total: float, width: int = 24,
         warn_at: float = 0.75, crit_at: float = 0.92) -> str:
    if total <= 0:
        return GREY + "·" * width + RESET
    frac = max(0.0, min(1.0, value / total))
    filled = int(round(frac * width))
    colour = GREEN if frac < warn_at else (YELLOW if frac < crit_at else RED)
    return colour + "█" * filled + GREY + "░" * (width - filled) + RESET


def _fmt(n: Any, digits: int = 0) -> str:
    if n is None:
        return "—"
    try:
        v = float(n)
    except (TypeError, ValueError):
        return str(n)
    if abs(v) >= 1e9:
        return f"{v/1e9:.2f}B"
    if abs(v) >= 1e6:
        return f"{v/1e6:.2f}M"
    if abs(v) >= 1e4:
        return f"{v/1e3:.1f}k"
    return f"{v:,.{digits}f}"


def _ms(v: Any) -> str:
    if v is None:
        return "—"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return "—"
    if v < 1000:
        return f"{v:.0f}ms"
    if v < 60000:
        return f"{v/1000:.1f}s"
    return f"{v/60000:.1f}m"


def render(broker_url: str, control_url: Optional[str], run_id: Optional[str]) -> str:
    out: List[str] = []
    health = _get(f"{broker_url}/health")
    status = _get(f"{broker_url}/v1/oe-max/status")

    # ------------------------------------------------------------ header
    ts = time.strftime("%H:%M:%S")
    if health is None:
        out.append(f"{BOLD}OE-MAX{RESET}  {RED}broker unreachable{RESET} "
                   f"at {broker_url}  {GREY}{ts}{RESET}")
        out.append(f"{GREY}start it with:  ./scripts/start-broker.sh{RESET}")
        return "\n".join(out)

    up = health.get("uptime_s") or 0
    out.append(
        f"{BOLD}OE-MAX{RESET}  broker {GREEN}up{RESET} {up/60:.0f}m   "
        f"served {BOLD}{_fmt(health.get('requests_served'))}{RESET}  "
        f"failed {_fmt(health.get('requests_failed'))}   "
        f"{GREY}{ts}{RESET}"
    )

    # --------------------------------------------------------- providers
    out.append(_rule("PROVIDERS"))
    for name, p in (health.get("providers") or {}).items():
        lim = p.get("limiter") or {}
        state = f"{GREEN}usable{RESET}" if p.get("usable") else f"{GREY}unusable{RESET}"
        key = "key" if p.get("key_present") else (
            f"{GREY}no key{RESET}" if p.get("requires_key") else f"{GREY}keyless{RESET}")
        line = f"  {name:<15} {state:<20} {key:<18} {DIM}{p.get('role','')}{RESET}"
        out.append(line)

        # The rolling-window count the spec calls out explicitly.
        if not lim.get("unlimited"):
            used, cap = lim.get("window_count", 0), lim.get("hard_cap", 0)
            out.append(
                f"    {DIM}rolling 60s{RESET} {_bar(used, cap)} "
                f"{used}/{cap}  headroom {lim.get('headroom', 0)}  "
                f"target {lim.get('target_rpm')} rpm"
                + (f"  {YELLOW}penalty ×{lim.get('penalty_factor')}"
                   f" ({lim.get('penalty_remaining_s')}s){RESET}"
                   if (lim.get("penalty_factor") or 1) > 1 else "")
            )
            st = lim.get("stats") or {}
            if st.get("throttle_events"):
                out.append(f"    {YELLOW}throttle events {st['throttle_events']}"
                           f"  waited {st.get('waited', 0)}×"
                           f"  max wait {st.get('max_wait_s', 0):.1f}s{RESET}")

    # ------------------------------------------------------------ routes
    out.append(_rule("ROUTES"))
    eligible = health.get("routes") or []
    if not eligible:
        out.append(f"  {RED}no eligible route{RESET}")
    for r in eligible[:6]:
        out.append(f"  {GREEN}●{RESET} {r}")
    if status:
        excluded = (status.get("router") or {}).get("excluded") or {}
        for label, why in list(excluded.items())[:6]:
            out.append(f"  {GREY}○ {label} — {why[:70]}{RESET}")

    # ------------------------------------------------------- route stats
    stats = (status or {}).get("stats_by_route") or {}
    if stats:
        out.append(_rule("REQUESTS BY ROUTE"))
        out.append(f"  {DIM}{'route':<38} {'reqs':>6} {'ok':>6} {'succ':>6} "
                   f"{'tokens':>9} {'avg':>8}{RESET}")
        for k, v in sorted(stats.items(), key=lambda kv: -kv[1]["requests"]):
            sr = v.get("success_rate", 0)
            colour = GREEN if sr >= 0.9 else (YELLOW if sr >= 0.5 else RED)
            out.append(
                f"  {k[:38]:<38} {v['requests']:>6} {v['ok']:>6} "
                f"{colour}{sr*100:>5.0f}%{RESET} {_fmt(v['tokens']):>9} "
                f"{_ms(v.get('avg_latency_ms')):>8}"
            )
            if v.get("errors"):
                errs = ", ".join(f"{k2}×{v2}" for k2, v2 in v["errors"].items())
                out.append(f"    {RED}{errs}{RESET}")

    # -------------------------------------------- reasoning-token warning
    recent = ((status or {}).get("router") or {}).get("recent_requests") or []
    reasoning = [r.get("reasoning_tokens") or 0 for r in recent]
    reasoning = [x for x in reasoning if x]
    if reasoning:
        avg = sum(reasoning) / len(reasoning)
        truncs = sum(1 for r in recent if r.get("outcome") == "truncated")
        out.append(_rule("REASONING BUDGET"))
        out.append(f"  hidden reasoning tokens/request: avg {_fmt(avg)} "
                   f"(max {_fmt(max(reasoning))})")
        if truncs:
            out.append(
                f"  {YELLOW}{truncs} truncated response(s) escalated to a larger "
                f"budget — raise llm.max_tokens if this persists{RESET}"
            )

    # ------------------------------------------------- evolution (if any)
    if control_url:
        runs = _get(f"{control_url}/api/query/runs")
        target = run_id
        if runs and not target:
            rs = runs.get("runs") or []
            if rs:
                target = rs[0]["run_id"]
        if target:
            s = _get(f"{control_url}/api/query/runs/{target}/summary")
            if s:
                out.append(_rule("EVOLUTION"))
                best = (s.get("best") or {}).get("combined_score")
                out.append(
                    f"  run {target[:16]}   gen {BOLD}{_fmt(s.get('generation'))}{RESET}"
                    f"   champion {BOLD}{GREEN}{best if best is None else f'{best:.5f}'}"
                    f"{RESET}"
                    f"   candidates {_fmt(s.get('candidates'))}"
                )
                ev = s.get("evaluations") or {}
                out.append(
                    f"  MAP-Elites {_fmt(s.get('map_elites_occupied'))} cells   "
                    f"islands {len(s.get('islands') or [])}   "
                    f"evals {_fmt(ev.get('total'))} "
                    f"({RED if ev.get('failed') else GREY}{_fmt(ev.get('failed') or 0)}"
                    f" failed{RESET})   "
                    f"tokens {_fmt(s.get('tokens'))}"
                )
                for i in (s.get("islands") or [])[:8]:
                    bs = i.get("best_score")
                    out.append(
                        f"    {DIM}island {i.get('island_id')}{RESET} "
                        f"pop {i.get('population'):<4} "
                        f"best {bs if bs is None else f'{bs:.5f}':<10} "
                        f"div {_fmt(i.get('diversity'), 1):<8} "
                        f"{DIM}↑{i.get('migrants_sent', 0)} "
                        f"↓{i.get('migrants_received', 0)}{RESET}"
                    )

                out.extend(_route_quality_lines(control_url, target))

    out.append(_rule())
    return "\n".join(out)


def _route_quality_lines(control_url: str, run_id: str) -> List[str]:
    """
    Mutation quality per route, alongside the reliability shown above.

    The two are different questions and were measured coming apart: Ox Alpha at
    26% success and a 284s p50 against a fallback at 100% and 112s. Reliability
    alone would say "switch"; whether the slower route's mutations are better is
    what this row answers.

    Rendered only when a route has attempts, and never with a recommendation
    the data does not support — the verdict comes from the tracker, which says
    when the evidence is too thin rather than ranking noise.
    """
    q = _get(f"{control_url}/api/query/runs/{run_id}/route-quality", timeout=10.0)
    routes = list((q or {}).get("routes", {}).values())
    if not routes:
        return []

    out = [_rule("ROUTE QUALITY")]
    out.append(f"  {DIM}{'route':<34}{'n':>4}{'fail':>6}{'dup':>6}"
               f"{'valid':>7}{'improv':>8}{'mean s':>9}{'impr/req':>11}{RESET}")
    for r in sorted(routes, key=lambda d: d.get("attempts", 0), reverse=True):
        mean = r.get("mean_latency_s")
        out.append(
            f"  {r['route'][:33]:<34}"
            f"{_fmt(r.get('attempts')):>4}"
            f"{(RED if r.get('failures') else GREY) + _fmt(r.get('failures')) + RESET:>{6 + len(RED) + len(RESET)}}"
            f"{_fmt(r.get('duplicates')):>6}"
            f"{r.get('validity_rate', 0) * 100:>6.0f}%"
            f"{r.get('improvement_rate', 0) * 100:>7.0f}%"
            f"{(f'{mean:.0f}' if mean else '-'):>9}"
            f"{r.get('improvement_per_request', 0):>11.4f}"
        )

    tp = (q or {}).get("throughput") or {}
    if isinstance(tp.get("candidates_per_request"), (int, float)):
        dup = tp.get("duplicate_share")
        dup_txt = f"{dup*100:.0f}%" if isinstance(dup, (int, float)) else "-"
        distinct = tp.get("useful_candidates_per_request")
        # Raw and distinct side by side on purpose: at N=3 the local run showed
        # 2.42 raw against 0.75 distinct, and the raw number alone reads as a
        # 2.4x win that is mostly the same program again.
        distinct_txt = (f"{distinct:.2f}/req"
                        if isinstance(distinct, (int, float)) else "-")
        dup_colour = YELLOW if isinstance(dup, (int, float)) and dup > 0.5 else GREY
        extra = tp.get("extra_offspring")
        line = (f"  {DIM}yield{RESET} {tp['candidates_per_request']:.2f}/req   "
                f"{DIM}distinct{RESET} {distinct_txt}   "
                f"{DIM}duplicates{RESET} {dup_colour}{dup_txt}{RESET}")
        if extra:
            line += f"   {DIM}extra offspring{RESET} {extra}"
        out.append(line)

    cov = (q or {}).get("coverage") or {}
    if isinstance(cov, dict) and cov.get("note"):
        out.append(f"  {GREY}{cov['note']}{RESET}")
    verdict = ((q or {}).get("comparison") or {}).get("verdict")
    if verdict:
        out.append(f"  {DIM}verdict:{RESET} {verdict}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(prog="oe-max-dashboard",
                                 description="Terminal dashboard for OE-MAX.")
    ap.add_argument("--broker", default=os.environ.get("OE_MAX_BROKER",
                                                       "http://127.0.0.1:8787"))
    ap.add_argument("--control", default=os.environ.get("EVOLUTION_CONTROL",
                                                        "http://127.0.0.1:8000"),
                    help="Control-plane URL for evolution state (optional).")
    ap.add_argument("--run", default=None, help="Run id; defaults to the newest.")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--once", action="store_true",
                    help="Print one frame and exit.")
    args = ap.parse_args()

    # Non-interactive output (a pipe, a log) gets one frame: repainting with
    # escape codes into a file is noise.
    once = args.once or not sys.stdout.isatty()

    try:
        while True:
            frame = render(args.broker.rstrip("/"),
                           args.control.rstrip("/") if args.control else None,
                           args.run)
            if once:
                print(frame)
                return 0
            sys.stdout.write("\033[2J\033[H" + frame + "\n")
            sys.stdout.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        sys.stdout.write("\n")
        return 0


if __name__ == "__main__":
    sys.exit(main())
