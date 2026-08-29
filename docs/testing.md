# Test Strategy

## Results

Measured 2026-08-27 on Windows 11 / CPython 3.11.9. Where a suite is
platform-sensitive the row says so, because a number with no platform attached
is the kind of thing that gets quoted back later as if it were universal.

| Suite | Command | Result |
|---|---|---|
| Upstream OpenEvolve | `pytest tests/ -m "not slow" --ignore=tests/{evolution,oe_max,brain}` | **431 passed**, 6 failed (all Windows-only, see below), 43 subtests, 17 slow deselected |
| Control plane | `pytest tests/evolution` | **456 passed**, 10 skipped |
| OE-MAX | `pytest tests/oe_max` | **381 passed**, 25 skipped |
| BrainPort | `pytest tests/brain` | **34 passed** |
| Web typecheck | `npm run typecheck` | clean |
| Web build | `npm run build` | 254 KB (75 KB gzipped) |
| Live UI | Chromium via Playwright, all 19 views as of that run | **0 JavaScript errors** |

`./test.sh` (or `.\test.ps1`) runs all four Python suites in that order.

## The six upstream failures on Windows

They are upstream's, they are platform assumptions rather than logic errors, and
they are **not** fixable here: `openevolve/` is byte-identical to upstream and
rule 1 says it stays that way. They are listed rather than skipped, because a
green suite that quietly hides six failures is worse than an honest six.

| Test | Cause |
|---|---|
| `test_valid_configs::test_import_config_files` | `openevolve/config.py:453` opens YAML with no `encoding=`, so Windows uses cp1252 and dies on a non-ASCII byte |
| `test_examples_validation::test_all_example_configs_load` | same |
| `test_api_key_from_env::test_yaml_file_loading_with_env_var` | same |
| `test_reasoning_effort_config::test_yaml_file_loading_with_reasoning_effort` | same |
| `test_template_dir_resolution::test_absolute_template_dir_unchanged` | asserts a POSIX absolute path survives unchanged; Windows resolves `/abs/path` to `C:\abs\path` |
| `test_process_parallel::test_controller_stop_terminates_running_workers` | expects `ProcessLookupError`, which is POSIX `os.kill` semantics |

All six pass on Linux, which is where the higher figure in earlier
handoffs was measured and what `bootstrap.sh` targets. Anything CI gates on
should run there. If you want them green on Windows, the fix belongs upstream
(pass `encoding="utf-8"`), not in this fork.

## Order matters

The upstream suite runs **first**. A change that breaks it is a regression in
the fork, not merely a control-plane bug — it is stronger evidence that the
engine is preserved than the byte-identical diff alone, because it exercises
behaviour rather than bytes.

For that reading to hold, `tests/` root must contain **only** upstream's files.
Everything the fork adds lives in `tests/evolution`, `tests/oe_max` or
`tests/brain`, and `tests/evolution/test_patch_surface.py` fails the build if
anything appears under `openevolve/` that upstream does not have.

## What the control-plane tests actually assert

Not coverage for its own sake — each targets a way the system could lie or
break.

**Redaction (12).** Key- and value-based matching; that operational fields like
`total_tokens` are *not* redacted; that redaction has already happened by the
time an event reaches a file sink; bounded recursion.

**Bus (7).** Events reach sinks; overflow drops are *counted* rather than
silent; a failing sink cannot break emission; sampling is deterministic; and —
the one that pins a real bug — a forked child rebuilds the bus rather than
inheriting a dead one. That test forks a real process and asserts both events
land.

**Store (13).** Idempotent ingest; MAP-Elites cells scoped per island; cell
replacement history; exclusive best flags; token throughput; evaluation
lifecycle merging; FTS indexing; rebuild-from-log reproducing projections
exactly; a torn final line not aborting a rebuild; and evaluation status
backfilling when evaluator events precede `candidate.created`.

**Providers (11).** Ox Alpha preferred for completions; **never** routed to
tool-requiring roles; the exclusion reason is explained; free status never
claimed permanent; missing credentials excluded with cause; circuit opens,
fails over and resets; concurrency shedding; every role has a valid chain.

**Isolation (9).** All owned paths inside the workspace; none overlapping
operator roots; writes into operator state refused; HOME and every XDG path
redirected; inherited `OPENCODE_*` variables *dropped* not overwritten;
preflight failing closed without a binary; OMO detection never raising when
absent.

**API (12).** Health and capabilities; unsupported controls declared with
reasons; missing files rejected 400; unknown runs 404; query endpoints returning
empty collections rather than fabricated placeholder rows; classic visualizer
reachable; isolation reported.

## End-to-end verification

A real OpenEvolve run through the fork, driven entirely by the control plane:

```
POST /api/control/runs → engine subprocess → telemetry → SQLite → API → UI
```

Verified on the `function_minimization` example against a local
OpenAI-compatible endpoint: 28 candidates, 27 lineage edges, 21 MAP-Elites cells
across 3 islands, 23 evaluations, 22 model calls, 14.0k tokens, 6 checkpoints,
261 stored events, fitness improving 1.36363 → 1.49873.

Then the UI itself was driven in Chromium: all 19 views that existed then
visited and screenshot (there are 20 now — the Memory view came later),
the command palette exercised, a candidate selected and every inspector tab
opened. Zero JavaScript errors.

Two real bugs were found this way and fixed:

- every candidate rendered `PENDING` despite 23 successful evaluations
  (evaluator events precede `candidate.created`)
- island diversity rendered `—` though upstream computes it

Both now have regression tests.

## Deliberately not tested

Stated rather than implied:

- **Live provider calls.** No `OPENCODE_API_KEY` or `NVIDIA_API_KEY` was
  available, so the doctor's live probes against Zen and NIM are **unverified**.
  The doctor's structure, capability filtering and failover logic are tested
  against profiles; the HTTP round-trip to those endpoints is not.
- **Sandbox execution.** The isolation boundary is tested, and so is the
  container backend's *argv construction*
  (`tests/oe_max/test_sandbox_mounts.py`). What is not covered on a machine
  without Docker is the container actually running: `tests/evolution/
  test_sandbox_eval.py` skips there, so that path first executed in CI — where
  it immediately failed twice, once on workdir permissions a non-root image
  cannot read and once on task files that were never mounted. Treat a green
  local run as saying nothing about the container backend
  ([sandbox.md](sandbox.md)).
- **Oh My OpenAgent integration.** Detection is tested for absence; no OMO
  install was available to test presence.
- **Windows.** No longer wholly unverified. Every `.ps1` script is parsed and
  gated by `tests/evolution/test_powershell_scripts.py` (16 tests), which also
  refuses a hardcoded `.venv\Scripts\python.exe` in favour of the shared
  resolver in `scripts/_common.ps1`, so the surface works under WSL and macOS
  too. `bootstrap.ps1`, `test.ps1` and
  `scripts/verify-brainport-acceptance.ps1` have been executed on Windows 11.
  The *operator* scripts that need a running broker — `start-broker.ps1`,
  `run-evolution.ps1`, `resume-evolution.ps1`, `ablation.ps1` — are parsed and
  argument-checked but have not been driven end-to-end against a live provider
  on Windows.
- **Worker telemetry off Linux.** Now covered, and it was not before: under
  `spawn` (the default on Windows and macOS) the pool initializer resolved to
  upstream's unwrapped function, so no worker emitted anything and the UI
  reported 0 model requests on a working run. Fixed and pinned by
  `tests/evolution/test_spawn_worker_telemetry.py`, and confirmed on a real
  12-iteration run: 1 emitting PID before, 3 after. The *fork* path remains
  covered separately by `test_bus_is_rebuilt_after_fork`.
- **Load at target scale.** Designed for tens of thousands of candidates with
  server-side pagination, downsampling and canvas rendering; verified runs were
  tens of candidates, not tens of thousands.
