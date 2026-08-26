#!/usr/bin/env python3
"""
Check every configured route against its provider, with nothing else running.

Both existing verification paths need a server up — `verify-providers.sh` talks
to the broker, and the Models page talks to the control-plane API. That is a
poor fit for the first question you have on arriving at this repo, which is
"does the routing table still describe reality?"

On 2026-08-26 the answer was no: four of the five configured remote routes were
dead simultaneously, the whole test suite passed, and finding out took an
afternoon of manual curl. This script is that afternoon, as one command:

    python3 scripts/check-models.py                 # everything
    python3 scripts/check-models.py --catalog-only  # no completions spent
    python3 scripts/check-models.py --no-tools      # skip the tools probes

Exit status is meant for CI as well as for reading:

    0   every enabled, probeable route answered
    1   at least one route failed a live probe
    2   at least one configured model id is absent from its provider's catalogue

2 outranks 1 deliberately. A failing probe may be an outage; a model that is no
longer listed is a configuration change you have to make.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from control_plane.providers.catalog import CatalogFetcher, CatalogStatus  # noqa: E402
from control_plane.providers.doctor import (  # noqa: E402
    ProbeResult, ProviderDoctor, apply_reports,
)
from control_plane.providers.profiles import (  # noqa: E402
    Role, default_profiles, default_role_chains,
)
from control_plane.providers.router import ModelRouter, NoRouteAvailable  # noqa: E402

GREEN, RED, YELLOW, DIM, BOLD, OFF = (
    ("\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m")
    if sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    else ("", "", "", "", "", "")
)

_MARK = {
    ProbeResult.PASS: f"{GREEN}pass{OFF}",
    ProbeResult.FAIL: f"{RED}FAIL{OFF}",
    ProbeResult.SKIPPED: f"{YELLOW}skip{OFF}",
    ProbeResult.UNKNOWN: "  ? ",
}
_CATALOG = {
    CatalogStatus.LISTED: f"{GREEN}listed{OFF}",
    CatalogStatus.ABSENT: f"{RED}ABSENT{OFF}",
    CatalogStatus.UNKNOWN: f"{YELLOW}unknown{OFF}",
}


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalog-only", action="store_true",
                    help="reconcile ids against each provider's /models listing "
                         "and stop — spends no completions")
    ap.add_argument("--no-tools", action="store_true",
                    help="skip the tool-calling probes")
    ap.add_argument("--timeout", type=float, default=90.0,
                    help="per-request timeout in seconds (default 90)")
    ap.add_argument("--tools-attempts", type=int, default=2,
                    help="tools probes per route; ALL must emit a tool call "
                         "(default 2 — a route that works one time in three "
                         "passes a single probe a third of the time)")
    args = ap.parse_args()

    profiles = default_profiles()
    enabled = [p for p in profiles if p.enabled]
    print(f"{BOLD}Checking {len(enabled)} enabled routes "
          f"({len(profiles) - len(enabled)} disabled){OFF}\n")

    fetcher = CatalogFetcher(timeout_s=args.timeout)

    if args.catalog_only:
        return await _catalog_only(profiles, enabled, fetcher)

    doctor = ProviderDoctor(timeout_s=args.timeout, catalog_fetcher=fetcher,
                            tools_probe_attempts=max(1, args.tools_attempts))
    reports = await doctor.check_all(profiles, probe_tools=not args.no_tools)
    apply_reports(profiles, reports)

    absent, failed = [], []
    for rep in reports:
        head = f"{BOLD}{rep.profile_id}{OFF}  {DIM}{rep.provider}/{rep.model}{OFF}"
        print(f"{head}\n  catalogue: {_CATALOG[rep.catalog_status]}  "
              f"{DIM}{rep.summary}{OFF}")
        for probe in rep.probes:
            print(f"    {_MARK[probe.result]}  {probe.name:<11} {probe.detail[:120]}")
        if rep.catalog_status is CatalogStatus.ABSENT:
            absent.append(rep)
            for s in rep.catalog_suggestions[:3]:
                print(f"    {YELLOW}try{OFF}   {s}")
        if not rep.available and any(
            p.result is ProbeResult.FAIL for p in rep.probes
            if p.name in ("chat", "chat_with_tools", "tools")
        ):
            failed.append(rep)
        print()

    _print_routes(profiles)
    return _verdict(absent, failed)


async def _catalog_only(profiles, enabled, fetcher) -> int:
    absent = []
    for prof in enabled:
        cat = await fetcher.get(prof.api_base, prof.secret_ref)
        status, detail = cat.status_for(prof.model)
        print(f"{_CATALOG[status]:<18} {prof.id:<34} {DIM}{prof.model}{OFF}")
        if status is CatalogStatus.ABSENT:
            absent.append(prof)
            for s in cat.suggestions(prof.model)[:3]:
                print(f"{'':<18} {YELLOW}try{OFF} {s}")
        elif status is CatalogStatus.UNKNOWN:
            print(f"{'':<18} {DIM}{detail[:110]}{OFF}")
    print()
    if absent:
        print(f"{RED}{len(absent)} configured model id(s) are not in their "
              f"provider's catalogue.{OFF}")
        print(f"{DIM}That is evidence, not proof — an unlisted preview can still "
              f"serve. Run without --catalog-only to find out.{OFF}")
        return 2
    print(f"{GREEN}Every configured model id is listed by its provider.{OFF}")
    return 0


def _print_routes(profiles) -> None:
    """What the router would actually pick, given what we just measured."""
    print(f"{BOLD}Selected route per role{OFF}")
    router = ModelRouter(profiles=profiles)
    for role in Role:
        try:
            chosen = router.select(role).id
            router.release(chosen, ok=True)
            print(f"  {role.value:<16} -> {chosen}")
        except NoRouteAvailable as exc:
            print(f"  {role.value:<16} -> {RED}NO ROUTE{OFF}")
            for pid, why in list(exc.reasons.items())[:4]:
                print(f"      {DIM}{pid}: {why[:100]}{OFF}")
    print()


def _verdict(absent, failed) -> int:
    if absent:
        print(f"{RED}{BOLD}{len(absent)} model id(s) absent from their provider's "
              f"catalogue:{OFF}")
        for rep in absent:
            print(f"  {rep.profile_id}  ({rep.model})")
        print(f"{DIM}Fix the ids in control_plane/providers/profiles.py. Take the "
              f"replacement from the provider's own /models listing, never from "
              f"memory — see DECISIONS D36.{OFF}")
        return 2
    if failed:
        print(f"{RED}{len(failed)} route(s) failed a live probe:{OFF}")
        for rep in failed:
            print(f"  {rep.profile_id}: {rep.summary[:110]}")
        print(f"{DIM}Ids are current, so this may be a transient outage. "
              f"Re-run before changing anything.{OFF}")
        return 1
    print(f"{GREEN}Every probeable route answered.{OFF}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130)
