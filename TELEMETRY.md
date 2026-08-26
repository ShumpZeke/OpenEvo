# Telemetry

## The rule

If a value appears in the UI, it traces to an emitted event. If the backend
cannot produce it, the value is shown as absent — never as zero, never as a
plausible-looking placeholder.

This is enforced structurally rather than by convention:

- The frontend has no fixture, seed or mock dataset. Every view reads
  `lib/api.ts`.
- Hooks return `{data, error, loading}` and views must render all three, so
  "failed to load" is visually distinct from "genuinely empty".
- Where a metric is unavailable — token usage from a provider that returns no
  `usage` block — the field is omitted from `metrics` rather than defaulted, and
  the UI renders `—`.

## Event model

`control_plane/telemetry/events.py`. Every family required by SOURCE_OF_TRUTH
section 8 is present: experiment, generation, candidate, population/archive,
island, migration, MAP-Elites, model, evaluator, sandbox, OpenCode, OMO,
checkpoint, resource, system and telemetry self-health.

```json
{
  "event_id": "ev_…", "trace_id": "…", "span_id": "…", "parent_span_id": null,
  "experiment_id": "exp_…", "run_id": "run_…",
  "generation": 3, "iteration": 12,
  "candidate_id": "…", "parent_candidate_ids": ["…"], "island_id": 1,
  "timestamp": 1787700498.68, "duration_ms": 84.2,
  "component": "database", "type": "map_elites.elite.replaced", "status": "ok",
  "summary": "cell 4-2 elite replaced",
  "input": {}, "output": {}, "metrics": {"score": 1.4987}, "metadata": {},
  "error": null, "pid": 13258
}
```

`pid` is recorded because a run spans several processes; `trace_id` groups a
run, `span_id`/`parent_span_id` nest units of work.

## Instrumentation

Hooks wrap public engine methods and read real state back afterwards — see
[PATCH_SURFACE.md](PATCH_SURFACE.md) for the full table. Two details worth
knowing:

**Fitness comes from upstream.** `_fitness()` calls
`openevolve.utils.metrics_utils.get_fitness_score`, so the UI's "combined score"
is the value the engine selected on, not a re-derivation that could drift.

**Token usage is captured at the client.** Upstream's `generate_with_context`
returns only the message text and discards the provider's `usage` block, so the
`_call_api` hook temporarily wraps the OpenAI client's `create` to capture the
raw response, then records `prompt_tokens`, `completion_tokens`,
`total_tokens`, `finish_reason` and the model the provider says it served
(which can differ from the one requested).

**Generation provenance crosses a process boundary.** Every candidate records
the model request that generated it — `gen_request_id`, `gen_provider`,
`gen_model`, `gen_latency_ms`, `gen_tokens` — which upstream does not provide
and no post-hoc join can recover (measured: 0 of 22 stored model requests
carried a candidate id).

Capturing it is not a matter of setting a variable, because in the default
`process_parallel` path the model request happens in a **worker process** and
`ProgramDatabase.add` happens in the **main** process, which receives only a
pickled `SerializableResult`. A ContextVar is correct inside the worker and
absent on the other side; a live run demonstrated it, producing 3 candidates
and 0 attributed. The attribution is therefore stamped onto `Program.metadata`
— a dataclass field that survives `to_dict()` → pickle → `Program(**dict)` — by
wrapping `_run_iteration_worker`, the only frame that spans both halves.

Within a worker iteration the *first* model request wins. The generating call
comes first by construction; a later one can only be the evaluator's LLM
feedback, and crediting a mutation to the request that judged it would be worse
than leaving it unattributed.

**Two cases are recorded as absent rather than guessed.** A migrant is a copy
of an already-attributed program — `_migrate_programs` copies metadata
wholesale, so without an explicit check one generation would be charged to its
route three times. A stale context means a checkpoint reload. Both come back
null, and `attribution_coverage` reports the shortfall with its reason, because
under the rule above a plausible attribution is worse than a missing one: the
missing one is visible.

**The recorded route is the one that served the request.** Through the broker
the engine only ever names the alias `oe-max-primary`, so the completed event
carries the provider and model from the broker's `oe_max` response stamp and
the alias is kept as `requested_model`. Without this every route through the
broker is one indistinguishable row.

## Transport

```
engine process ─┬─► NDJSON append  (durable, replayable, per run)
                └─► loopback TCP   (best effort, low latency)
                                    │
worker processes ── same, after fork-safe re-init
                                    ▼
                     collector (API process, single SQLite writer)
                     tails every log AND accepts the socket
                     ingest is idempotent on event_id
                                    ▼
                    events table ──► projections ──► query API ──► SSE
```

The socket is best effort by design: if the control plane is not running,
evolution proceeds untouched and the log still has everything.

### Fork safety

Worker processes are forked and inherit the bus object but not its worker
thread. Both the bus and the instrumentation are keyed by owning PID and rebuilt
in the child; `os.register_at_fork` clears the inherited globals eagerly. The
child drops the inherited bus *without closing it*, because the parent still
owns those handles.

Without this, worker telemetry — every model call and most evaluations — is
lost. See [DECISIONS.md](DECISIONS.md) D4.

## Performance

Emission is a bounded queue append: no disk, socket or database work on the
engine's thread. A background worker batches to sinks. A failing sink is
contained and, after repeated failures, disabled and reported — never propagated
into the engine.

Under pressure the queue drops and **counts** the drop. `dropped_overflow`
appears on the System page, and the status bar shows a live indicator, so a
gappy live feed is visible rather than silently misleading. Stored history is
unaffected: drops only affect the live tail.

High-volume, low-value families (`resource.*`, `evaluator.stdout/stderr`,
`sandbox.process`) are sampleable. Sampling is deterministic decimation, not
RNG, so a run stays reproducible. Decision-carrying events are never sampleable.

## Storage

Two tiers (`storage/schema.py`):

- **`events`** — append-only, never updated. The record.
- **projections** — `candidates`, `islands`, `map_elites_cells`,
  `map_elites_history`, `evaluations`, `model_requests`, `migrations`,
  `checkpoints`, `resource_metrics`, `sandbox_runs`, `agent_runs`. Current state
  the UI queries directly.

Projections are a cache, not a second source of truth:

```python
store.rebuild_projections_from_log("events.ndjson")
```

drops and replays them exactly. That is the recovery path if the database is
lost, and the migration path when a projection bug is fixed retroactively. A
torn final line from a killed process is skipped rather than aborting the
rebuild.

Candidate *code* is not duplicated into projections — it lives in the event log
and in upstream's own checkpoints, and the candidate endpoint reads it back from
the originating event.

## Self-health

Telemetry reports on itself: emitted, delivered, queue depth and peak, drops by
cause, sink errors, emit rate, plus collector counters (received, ingested,
duplicates skipped, parse errors, pending, subscribers, tailed logs). All of it
is on the System page. A `telemetry.health` event is emitted periodically so
self-health is itself part of the record.
