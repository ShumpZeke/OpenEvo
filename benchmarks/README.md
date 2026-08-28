# Benchmarks

Two harnesses live here. **Neither of them calls a real model**, and the JSON
they write says so in its own first field. Read this before quoting a number
out of either file.

| File | What it drives | What the numbers mean |
|---|---|---|
| `benchmark_brainport.py` → `results.json` | the BrainPort plumbing under `NullBrainPort` | cost and bookkeeping of the port itself |
| `bench_alpha.py` → `alpha_results.json` | the real evolution loop under `SimulatedBrain` | the loop's accounting — archive, cache, dedup, gates |

## What these do and do not establish

They **do** establish that the machinery is correct and cheap: that duplicates
are rejected before evaluation, that the content cache is consulted, that the
archive only grows on a genuine improvement, and that a run terminates on its
budget. Those are properties of our code, so a stub brain is the right
instrument — it removes the model as a variable and makes the run reproducible
in under a second.

They **do not** establish anything about evolution quality. `best_score`,
`improvements` and `archive_improvement` in these files are properties of the
canned responses, not of a search. `SimulatedBrain` returns strings on a fixed
10% invalid / 10% duplicate / 80% valid split; the score it climbs is arithmetic
on a counter. Quoting `best_score: 2.0` as a result would be quoting the
fixture.

The live numbers this project actually has — real provider, real latency, real
success rates — are in [`../docs/benchmarks.md`](../docs/benchmarks.md) at the repository
root, and they are measured through the legacy broker rather than through the
BrainPort. **No BrainPort run against a real model has been recorded yet.** That
gap is deliberate to state rather than paper over; it is why
`scripts/verify-brainport-acceptance.ps1` reports it as UNVERIFIED instead of
counting the presence of `results.json` as evidence.

## Running them

```bash
python benchmarks/benchmark_brainport.py --iterations 20 --seed 42
python benchmarks/bench_alpha.py
```

Both are deterministic given their seeds, so a changed number means the code
changed. That is the whole point of a stub: it makes the harness a regression
test rather than a weather report.
