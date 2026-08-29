# fn_min_seeded — function minimization with the randomness pinned

The same task as `examples/function_minimization`, with one change: the
evaluator fixes the random draws, so the same program always scores the same
number.

## Why it was needed

The upstream evaluator runs the candidate ten times and averages. The candidate
draws from an unseeded RNG, so the average is a random variable. Measured on the
**unchanged seed program**, five evaluations apart:

| evaluator | run 1 | run 2 | run 3 | run 4 | run 5 | spread |
|---|---|---|---|---|---|---|
| `examples/function_minimization` | 1.4188 | 1.4030 | 1.4157 | 1.4063 | 1.0330 | **0.3857** |
| this one | 1.4061 | 1.4061 | 1.4061 | 1.4061 | 1.4061 | **0.0000** |

A 0.39 spread is larger than most real improvements on this task, so any single
run comparing two programs was measuring the dice. That is not a theoretical
worry — a 12-iteration run here reported *"new best solution found at iteration
9"* and finished with a best program **byte-identical to the seed**. The
"improvement" was the same code drawing a luckier sample.

Reproduce both rows:

```bash
python benchmarks/tasks/fn_min_seeded/check_determinism.py
```

```bash
python benchmarks/tasks/fn_min_seeded/check_determinism.py --upstream
```

It exits non-zero when the evaluator under test is not deterministic, so it
works as a gate. The pass condition is a spread of exactly zero — "small" is not
good enough, because the point is that a score difference must be attributable
to the program rather than to the draw.

## Two things this buys

**Reliable single runs.** One evaluation now answers what previously took
several, which is the cheaper half of every experiment on this box.

**A paired comparison.** Two programs meet the *same ten* draws rather than two
independent samples, so the draw-to-draw variance drops out of the difference as
well. A much smaller genuine improvement becomes visible.

## What it found immediately

`compare.py` prints the ten trials behind the aggregate:

```bash
python benchmarks/tasks/fn_min_seeded/compare.py \
    benchmarks/tasks/fn_min_seeded/initial_program.py \
    benchmarks/tasks/fn_min_seeded/refined_program.py
```

`refined_program.py` spends 70% of its budget exploring like the seed and the
rest refining around the best point. It lands closer to the global minimum on
**9 of 10 seeds**, typically 0.03 against the seed's 0.19 — roughly five times
closer. It scores **lower**: 1.3564 against 1.4061.

On seed 67 it refines into the wrong basin and finishes 3.99 away. Since
`combined_score` averages distances, that one trial moves the mean from 0.19 to
0.43 and outweighs nine near-perfect ones.

So the metric is mean-based and outlier-dominated: it prefers a program that is
mediocre everywhere to one that is excellent nine times out of ten. Evolution
optimises the score, so it will reject the refinement.

The weights are **deliberately left identical to upstream's** — the job of this
directory is to make the existing metric measurable, not to substitute a metric
that flatters a particular answer. Recording the property is the useful part.
`refined_program.py` exists as the worked example of it and is not a target to
beat.

## The score can be bought with compute

Nothing here scores runtime, so the cheapest available improvement is to sample
more. Measured on the seed program with only the budget changed:

| `iterations` | `combined_score` | evaluation |
|---|---|---|
| 500 | 0.9875 | 0.06 s |
| 1000 (seed) | 1.4061 | 0.04 s |
| 2000 | 1.4513 | 0.06 s |
| 5000 | 1.4600 | 0.16 s |
| 20000 | 1.4805 | 0.57 s |

It is bounded only by the 5-second trial timeout, and the returns diminish, but
it is free score for no algorithmic insight.

This is not hypothetical. The first real run against this task -- 30 iterations
of `qwen3:0.6b`, about three minutes -- produced exactly one accepted change:

```diff
-def search_algorithm(iterations=1000, bounds=(-5, 5)):
+def search_algorithm(iterations=2000, bounds=(-5, 5)):
```

1.406051 to 1.451256, reproducible on re-scoring. A genuine improvement on the
metric, and the laziest one available.

**So read a score rise here as evidence the loop works, not as evidence the
model can improve an algorithm.** Check `average_seconds` in the artifacts
beside the score: a candidate that bought its score with sampling took longer
to evaluate, and that shows up there.

Like the averaging behaviour above, this is a property of upstream's metric,
recorded rather than fixed -- reweighting would make every number already
recorded incomparable.

## Running evolution against it

```bash
python openevolve-run.py benchmarks/tasks/fn_min_seeded/initial_program.py benchmarks/tasks/fn_min_seeded/evaluator.py --config configs/oe_max/local.yaml
```

Scores from this task are comparable to each other. They are **not** comparable
to numbers from `examples/function_minimization`: fixing the draws makes this
one particular sample of the upstream metric, not an estimate of its mean.

## What is pinned, and what is not

Pinned: `random`, NumPy's legacy global, and the three NumPy entry points that
silently reach for OS entropy when called with no argument — `default_rng()`,
`RandomState()` and `PCG64()`. Candidates are also re-imported per trial, so a
generator built at module scope is rebuilt under that trial's seed instead of
every trial sharing the first one.

Not pinned, and still able to make a score vary: `secrets` and `os.urandom`; a
hand-rolled clock seed such as `default_rng(int(time.time()))`;
wall-clock-dependent logic such as "stop after N seconds"; and set/dict
iteration order across processes via `PYTHONHASHSEED`.

One more, and it is the evaluator's own: each trial has a five-second wall-clock
limit. A candidate whose trials land near it can finish on an idle machine and
time out on a busy one. It stays because the alternative is an unbounded loop
hanging the run for good, and because nothing near that boundary is a program
worth keeping — the seed's trials are about three milliseconds. The limit bounds
the *wait*, not the work: Python cannot kill a thread, so a timed-out trial keeps
running in the background until it finishes on its own.

Wall clock is reported in the artifacts but deliberately **not** scored — it is
the one quantity here that is not reproducible, and folding it into
`combined_score` would put the noise straight back in.

Rather than assume none of that is biting, `check_determinism.py` measures it.

## Files

| file | role |
|---|---|
| `initial_program.py` | seed; same algorithm as upstream's, duplicated because evolution rewrites it and `examples/` must stay byte-identical |
| `evaluator.py` | the deterministic evaluator; same metric names and weights as upstream |
| `check_determinism.py` | proves the spread is zero; gate-able |
| `compare.py` | paired per-seed comparison of two programs |
| `refined_program.py` | worked example of the outlier sensitivity described above |

`evaluator.py` also defines `evaluate_stage1` / `evaluate_stage2`, because the
shipped local config enables cascade evaluation and an evaluator without them
makes that setting silently useless. Stage 1 screens on three seeds; stage 2
is the full ten. The engine merges stage 2 over stage 1, so a cascade run and
a direct run report the **same** number — a test holds that, because
otherwise a config flag would quietly make two runs incomparable.
