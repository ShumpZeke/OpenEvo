# Test Strategy

## Results

| Suite | Command | Result |
|---|---|---|
| Upstream OpenEvolve | `pytest tests/ -m "not slow" --ignore=tests/evolution` | **437 passed**, 43 subtests, 17 slow deselected |
| Control plane | `pytest tests/evolution` | **81 passed** |
| Web typecheck | `npm run typecheck` | clean |
| Web build | `npm run build` | 254 KB (75 KB gzipped) |
| Live UI | Chromium via Playwright, all 19 views | **0 JavaScript errors** |

`./test.sh` runs all of them.

## Order matters

The upstream suite runs **first**. A change that breaks it is a regression in
the fork, not merely a control-plane bug. Those 437 tests are the contract that
the engine is genuinely preserved — stronger evidence than the byte-identical
diff alone, because they exercise behaviour rather than bytes.

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

Then the UI itself was driven in Chromium: all 19 views visited and screenshot,
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
- **Sandbox execution.** The isolation boundary is tested; the executors do not
  exist to test ([SANDBOX.md](SANDBOX.md)).
- **Oh My OpenAgent integration.** Detection is tested for absence; no OMO
  install was available to test presence.
- **Windows.** The `.ps1` scripts mirror the shell scripts and the code paths
  avoid POSIX-only assumptions (control file rather than `SIGUSR1`, TCP rather
  than AF_UNIX), but they have not been executed on Windows.
- **Load at target scale.** Designed for tens of thousands of candidates with
  server-side pagination, downsampling and canvas rendering; verified runs were
  tens of candidates, not tens of thousands.
