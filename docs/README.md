# Documentation

Start with the [README](../README.md) for what this is and how to run it. This
directory is the detail.

## If you are building on it

| | |
|---|---|
| [architecture.md](architecture.md) | how the pieces fit, and why the engine is untouched |
| [gotchas.md](gotchas.md) | **defects that produced no error, or the wrong one — read before debugging** |
| [local-mode.md](local-mode.md) | running entirely on your own machine, with no credentials |
| [local-tuning.md](local-tuning.md) | **fitting a model to your GPU: a measured worked example** |
| [providers.md](providers.md) | routing policy, and what each provider actually does |
| [telemetry.md](telemetry.md) | the event model, and the rule that nothing is invented |
| [sandbox.md](sandbox.md) | candidate isolation, and what it does and does not contain |
| [scientific-tools.md](scientific-tools.md) | local computation, and why a result is not a boolean |
| [testing.md](testing.md) | what is covered, what is not, and the six Windows failures |

## If you are designing it

| | |
|---|---|
| [design/control-center.md](design/control-center.md) | the browser UI: colour, type, primitives, the twenty views, and the no-invented-data rule |

## If you are merging upstream

| | |
|---|---|
| [patch-surface.md](patch-surface.md) | every upstream file this fork modifies — currently none, and how that is kept true |
| [upstream-sync.md](upstream-sync.md) | pulling a new upstream release |

## Record

| | |
|---|---|
| [decisions.md](decisions.md) | engineering decisions and the evidence behind them |
| [benchmarks.md](benchmarks.md) | measurements, with the conditions they were taken under |
| [project/status.md](project/status.md) | what is built, what is verified, what is not |
| [project/roadmap.md](project/roadmap.md) | the prioritised queue, with rationale and counter-arguments |
| [project/requirements.md](project/requirements.md) | spec coverage |
| [project/coverage.md](project/coverage.md) | feature-by-feature status |
| [project/build-log.md](project/build-log.md) | what was built when, and what each measurement changed |
| [project/handoff.md](project/handoff.md) | continuity notes for whoever picks the work up next |
| [UPSTREAM_CLAUDE.md](UPSTREAM_CLAUDE.md) | upstream's own agent instructions, kept for reference |

---

Two conventions this documentation follows, because they are the same ones the
code follows:

**A claim carries a number or the word "unverified".** Where something has not
been measured, the document says so rather than implying it works. A label that
is stale in the optimistic direction and one that is stale in the pessimistic
direction are the same defect.

**Say why, not just what.** Most of these files explain a decision that looks
wrong until you know what it cost to learn. That context is the point — the
alternative is someone helpfully "fixing" it back.
