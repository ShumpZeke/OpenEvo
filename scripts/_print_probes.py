"""Render provider verification results as a table (used by verify-providers.sh)."""
import json
import sys

d = json.load(sys.stdin)
print("discovered:", d.get("discovered_counts"))
print()
header = "{:<15}{:<32}{:<7}{:<8}{:>10}  {}".format(
    "provider", "model", "serves", "tools", "latency", "detail")
print(header)
print("-" * len(header))
for p in d.get("probes", []):
    lat = f"{p['latency_ms']:.0f}ms" if p.get("latency_ms") else "-"
    print("{:<15}{:<32}{:<7}{:<8}{:>10}  {}".format(
        p.get("provider", "")[:14],
        str(p.get("model", ""))[:31],
        str(p.get("reachable")),
        str(p.get("supports_tools")),
        lat,
        str(p.get("detail", ""))[:52],
    ))
print()
print("eligible routes:", d.get("eligible_routes"))
