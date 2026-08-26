"""
Render provider verification results as a table.

Shared by `verify-providers.sh` and `verify-providers.ps1`, so both platforms
report the same thing rather than drifting apart.

Reconciliation is printed first and deliberately: it is the check that answers
"is a configured model still listed?", and the one that would have caught the
withdrawn primary route on the day it disappeared instead of weeks later. A
smoke-test table full of failures tells you something is wrong; a
reconciliation line tells you what.
"""
import json
import sys

d = json.load(sys.stdin)

counts = d.get("discovered_counts") or {}
listed = {k: v for k, v in counts.items() if v}
print("discovered:", listed or "nothing — no provider returned a listing")
if not listed:
    print("  (a provider with no credential is skipped unless its listing is public)")

changes = d.get("reconciled") or {}
if changes:
    print()
    print("RECONCILED against the live listings:")
    for route, what in changes.items():
        print(f"  {route}  ->  {what}")

sizes = d.get("chain_sizes") or {}
if sizes:
    print()
    print("routes per role chain:",
          ", ".join(f"{role} {n}" for role, n in sizes.items()))

print()
header = "{:<15}{:<32}{:<7}{:<8}{:>10}  {}".format(
    "provider", "model", "serves", "tools", "latency", "detail")
print(header)
print("-" * len(header))

probes = d.get("probes", [])
inconclusive = 0
for p in probes:
    lat = f"{p['latency_ms']:.0f}ms" if p.get("latency_ms") else "-"
    tools = p.get("supports_tools")
    # `None` here does not mean "no". It means this probe could not tell —
    # a 503 on the tools call says nothing about whether the model supports
    # tools — so the previously measured value was kept. Rendering it as
    # "False" would be the bug this column exists to avoid.
    tools_cell = {True: "yes", False: "NO"}.get(tools, "?")
    if not p.get("reachable") and not p.get("conclusive", True):
        inconclusive += 1
    print("{:<15}{:<32}{:<7}{:<8}{:>10}  {}".format(
        p.get("provider", "")[:14],
        str(p.get("model", ""))[:31],
        "yes" if p.get("reachable") else "no",
        tools_cell,
        lat,
        str(p.get("detail", ""))[:52],
    ))

serving = [p for p in probes if p.get("reachable")]
if probes:
    print()
    print(f"{len(serving)} of {len(probes)} probed models actually served.")
    print("tools '?' = this probe could not tell; the previous measurement was kept.")
    if inconclusive:
        print(f"{inconclusive} failure(s) were inconclusive (a timeout or 5xx says "
              f"nothing about the model) and did NOT demote a route.")

print()
print("eligible routes:", d.get("eligible_routes"))
