# Architecture

## The organising constraint

OpenEvolve remains the evolutionary authority. It owns selection, parent choice,
candidate identity, generations, islands, migration, MAP-Elites placement,
archive and best tracking, checkpointing, and fitness ingestion. The control
plane observes and controls it; it never makes an evolutionary decision.

That boundary is what keeps the system comprehensible. A control plane that
started re-deriving fitness, or re-implementing selection, would produce a UI
that disagrees with the engine — and the engine would still be right.

```
                      ┌──────────────────────────────┐
                      │   Browser Control Center     │
                      │   20 views, SSE live stream  │
                      └───────────────┬──────────────┘
                                      │ REST + SSE
                      ┌───────────────┴──────────────┐
                      │        Control plane          │
                      │  ┌────────┬────────┬───────┐  │
                      │  │ control│ query  │stream │  │
                      │  └────┬───┴───┬────┴───┬───┘  │
                      │  ┌────┴───────┴────────┴───┐  │
                      │  │  collector → SQLite     │  │
                      │  │  (events + projections) │  │
                      │  └────────────▲────────────┘  │
                      │  ┌────────────┴────────────┐  │
                      │  │ runner · providers ·    │  │
                      │  │ sandbox isolation       │  │
                      │  └────────────┬────────────┘  │
                      └───────────────┼───────────────┘
                        spawn         │      NDJSON + loopback TCP
                      ┌───────────────▼───────────────┐
                      │   engine subprocess            │
                      │   runtime hooks installed      │
                      │  ┌──────────────────────────┐  │
                      │  │  OpenEvolve (unmodified) │  │
                      │  │  LLMs · MAP-Elites ·     │  │
                      │  │  islands · evaluators    │  │
                      │  └────────────┬─────────────┘  │
                      │      ProcessPoolExecutor       │
                      │      workers (also hooked)     │
                      └────────────────────────────────┘

  Preserved alongside, untouched:
      openevolve-run CLI              classic Flask visualizer (:8080)
```

## Data flow

1. **Emit.** Hooks wrap public engine methods and read real state back after the
   call. Emission is a bounded, non-blocking queue append.
2. **Redact.** Every event passes the redactor before reaching any sink, so a
   credential that entered a prompt or an error message never lands on disk.
3. **Transport.** Each process appends NDJSON to the run's durable log and, when
   a collector is listening, mirrors to a loopback TCP socket for low latency.
4. **Ingest.** The API process is the single SQLite writer. It tails every run's
   log *and* accepts the socket stream; ingest is idempotent on `event_id`, so
   the overlap is free and a control plane that was down backfills on restart.
5. **Project.** Events fold into current-state tables (candidates, islands,
   cells, evaluations, model requests). Projections are a cache: dropping them
   and replaying the log reconstructs them exactly.
6. **Serve.** Query endpoints read projections; the SSE endpoint fans out live
   batches.

The durable log is the source of truth. SQLite is an index over it.

## Why these choices

**Runtime wrapping, not source edits.** Keeps the patch surface empty so
upstream merges fast-forward. Cost: hooks depend on method names. Benefit: they
degrade to a logged warning instead of a merge conflict. See
[patch-surface.md](patch-surface.md).

**Single writer, many readers.** SQLite's weakness is concurrent writers, so
only the API process writes. Engine and workers never open the database. WAL
gives readers concurrency without blocking the writer.

**Two transports.** The socket alone loses events whenever the control plane is
restarting; the log alone adds tailing latency. Together they give live latency
with durability, and idempotent ingest makes the redundancy harmless.

**Bounded queue that drops and counts.** Telemetry must never throttle
evolution, so back-pressure onto the engine is not an option. Dropping is —
provided the drop is counted and surfaced. The System page shows the counter and
the status bar shows a live drop indicator, so a gappy feed is visible rather
than silently misleading.

**Fork-safe bus.** `ProcessPoolExecutor` workers are forked on POSIX and inherit
the bus object but not its worker thread. Both the bus and the instrumentation
are keyed by owning PID and rebuilt in the child. Without this, all worker
telemetry — every model call and most evaluations — queues into a buffer nothing
drains and is lost. This was a real bug found during integration testing, and
`tests/evolution/test_bus.py::test_bus_is_rebuilt_after_fork` pins it.

**Canvas for the lineage graph.** One DOM node per candidate stops scaling long
before the data does. A canvas draws 20k nodes per frame and keeps pan/zoom
responsive. Layout is deterministic (x = iteration, y = island band + stable id
hash) so nodes do not jump as new candidates stream in during a live run.

## Component map

| Module | Responsibility |
|---|---|
| `telemetry/events.py` | typed event model, every family in section 8 |
| `telemetry/redaction.py` | key- and value-based secret removal, pre-persistence |
| `telemetry/bus.py` | bounded queue, batching, sinks, fork safety, self-health |
| `telemetry/instrument.py` | engine hooks; reads real state back |
| `telemetry/collector.py` | socket server + log tailing → single SQLite writer |
| `storage/schema.py` | events + projections; MAP-Elites keyed per island |
| `storage/store.py` | idempotent ingest, projections, replay rebuild |
| `providers/profiles.py` | model profiles, capabilities, free status |
| `providers/doctor.py` | live probes: reachability, auth, latency, tool support |
| `providers/router.py` | capability filter → health sort → circuit breaker |
| `sandbox/opencode.py` | isolation boundary; refuses operator-owned paths |
| `runner/manager.py` | subprocess lifecycle, real controls, capability reporting |
| `runner/entrypoint.py` | installs hooks, then runs upstream's own CLI |
| `api/app.py` | control, query, SSE, classic-visualizer bridge |
| `telemetry/multi_offspring.py` | splits N alternatives before upstream's parser sees them |
| `telemetry/sandbox_eval.py` | replaces in-process evaluation with a sandboxed one |
| `telemetry/verification_hook.py` | decides which candidates are worth verifying |
| `telemetry/seed_hook.py` | turns the first program into a starting population |
| `analysis/route_quality.py` | stored telemetry → per-route mutation quality |
| `analysis/throughput.py` | candidates per request, and how many are distinct |
| `analysis/outcome.py` | best-so-far against requests; area under it |
| `oe_max/route_quality.py` | what the quality measures mean |
| `oe_max/verification/` | V1 property/metamorphic/randomized checks; counterexamples |
| `oe_max/execution/` | resource-capped candidate execution, backends and their gaps |
| `oe_max/search/policies.py` | per-island search posture over operator disruption |
| `oe_max/search/seed_forge.py` | a starting population from one seed, no model requests |

## Scaling

Section 25 targets tens of thousands of candidates and hundreds of thousands of
events. Concretely:

- Every list endpoint is paginated and filtered in SQL, never in the client.
- The lineage endpoint is server-capped and reports `truncated` so the UI can
  say so rather than silently showing a subset.
- Resource series are bucketed and averaged server-side before transport.
- Indexes cover the access patterns the UI actually uses (`run_id + seq`,
  `run_id + score DESC`, `run_id + island`, `candidate_id`).
- High-volume, low-value families (`resource.*`, `evaluator.stdout`) are
  sampleable by deterministic decimation, and `prune_events` trims them by age.
- The SSE queue is bounded per subscriber; a slow tab drops and is told.

## What is not built

Stated plainly rather than stubbed:

- **The container execution backend, verified.** `oe_max/execution/` implements
  both backends and candidates now run under real ceilings
  (`OE_MAX_SANDBOX_EVAL`). The subprocess backend is exercised by tests that
  actually try to exceed each limit. The container backend — the one that adds
  network and filesystem isolation — has **never run**: `docker` is on PATH
  here and its daemon is unreachable, which the probe reports rather than
  assuming. `describe_backends()` states what each backend does not stop.
- **The operator bandit, in the loop.** `search/bandit.py` is built and tested;
  operator selection is uniform random. The bandit learns from per-operator
  reward, and there was none until mutations could be labelled — that data is
  only now starting to exist.
- **Oh My OpenAgent orchestration.** Detection is implemented and deliberately
  does not hardcode a package name; the orchestration layer is not built.
- **Alert engine.** The `alerts` table and thresholds are designed; no evaluator
  runs them yet.
- **Pause/resume in place, fork-from-candidate.** Upstream cannot support these;
  they are reported unsupported with reasons and disabled in the UI.
