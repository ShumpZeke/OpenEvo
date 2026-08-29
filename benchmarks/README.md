# Benchmarks

Two stub harnesses and one real task live here.

The harnesses **do not call a model**, and the JSON they write says so in its
own first field. Read this before quoting a number out of either file.

| File | What it drives | What the numbers mean |
|---|---|---|
| `benchmark_brainport.py` → `results.json` | the BrainPort plumbing under `NullBrainPort` | cost and bookkeeping of the port itself |
| `bench_alpha.py` → `alpha_results.json` | the real evolution loop under `SimulatedBrain` | the loop's accounting — archive, cache, dedup, gates |
| `tasks/fn_min_seeded/` | a real evolution task, real model | search quality — see below |

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

## `tasks/fn_min_seeded` — the one that measures search quality

This one *is* driven by a real model. It is
`examples/function_minimization` with the random draws pinned, and it exists
because the upstream task cannot support a comparison: the **unchanged seed
program** scores anywhere in 1.0330–1.4188 there, a spread of 0.39, which is
wider than most real improvements. A run once reported a new best and finished
with a program byte-identical to the seed.

With the draws fixed the same program scores 1.406051 every time — twenty
evaluations, spread exactly 0.0. Two programs also meet the *same* ten draws,
which makes the comparison paired and lets a much smaller genuine improvement
show up.

Read [`tasks/fn_min_seeded/README.md`](tasks/fn_min_seeded/README.md) before
quoting anything from it. In particular, its scores are **not** comparable to
`examples/function_minimization`, and its metric — upstream's, deliberately
unmodified — averages over trials, so it prefers a program that is mediocre
everywhere to one that is excellent on nine seeds and trapped on the tenth.

## Running them

```bash
python benchmarks/benchmark_brainport.py --iterations 20 --seed 42
```

```bash
python benchmarks/bench_alpha.py
```

Both are deterministic given their seeds, so a changed number means the code
changed. That is the whole point of a stub: it makes the harness a regression
test rather than a weather report.

The seeded task is deterministic in the same sense, and says so out loud:

```bash
python benchmarks/tasks/fn_min_seeded/check_determinism.py
```

It exits non-zero if the evaluator under test is not deterministic.
