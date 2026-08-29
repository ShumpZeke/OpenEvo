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
