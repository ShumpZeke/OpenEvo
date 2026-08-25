# Decisions

Engineering decisions, the evidence behind them, and what would change them.

---

## D1 — Instrument by runtime wrapping, not source edits

**Decision.** Telemetry wraps public engine methods at runtime. `openevolve/`
stays byte-identical to upstream.

**Why.** The fork must keep merging upstream (section 27). Editing call sites in
`database.py`, `evaluator.py` and `controller.py` creates a diff in precisely
the functions upstream changes most, so every release becomes a conflict.

**Cost, stated honestly.** Hooks depend on method *names*. If upstream renames
`ProgramDatabase.add`, that hook silently stops firing. Mitigated by: `_patch()`
logging a warning when its target is missing, and control-plane tests asserting
that each hook still produces its events, so a rename fails CI.

**Would change if.** Upstream adopted a real observability hook API — then we
would use it and delete the wrappers.

---

## D2 — Read engine state back rather than predicting it

**Decision.** Hooks call the original method, then read `island_feature_maps`,
`programs`, `best_program_id` and island membership to see what actually
happened.

**Why.** Predicting outcomes means reimplementing MAP-Elites placement, novelty
rejection and population-limit eviction in a second place. Those would drift,
and the UI would confidently show the wrong occupant. Measuring cannot drift.

Fitness specifically uses upstream's own `get_fitness_score`, so the number the
UI calls "combined score" is the number selection actually used.

---

## D3 — MAP-Elites cells are keyed per island

**Decision.** Primary key `(run_id, island_id, cell_key)`.

**Why.** Upstream keeps a *separate* feature map per island
(`island_feature_maps[i]`). Keying on `cell_key` alone collapses islands into
one grid and shows the wrong elite. Found during integration testing: a 3-island
run reported 3 cells instead of 5. Pinned by
`test_map_elites_cells_are_scoped_per_island`.

---

## D4 — The bus is keyed by owning PID

**Decision.** `configure_bus` and `auto_install_from_env` rebuild when the PID
differs; `os.register_at_fork` clears the inherited globals in the child.

**Why.** OpenEvolve evaluates in a `ProcessPoolExecutor`. On POSIX those workers
are forked, and a forked child inherits the `EventBus` object but **not** its
worker thread — threads do not survive `fork()`. The child then saw
`_bus is not None`, skipped setup, and queued every event into a buffer nothing
drained.

**Evidence.** Before the fix, an integration run reported `model_requests: 0`
and 1 evaluation despite 12 iterations. After: 10 model requests, 11
evaluations. Pinned by `test_bus_is_rebuilt_after_fork`.

The child drops the inherited bus without closing it — closing would corrupt the
parent's still-open file handles.

---

## D5 — Ox Alpha is preferred for completions and excluded from tool roles

**Decision.** `zen-ox-alpha-free` is priority 0 and leads every completion-only
chain. Tool-requiring roles lead with a verified tools-capable model.

**Evidence.**
[anomalyco/opencode#44300](https://github.com/anomalyco/opencode/issues/44300),
open at time of writing: `x-preview-f-free` fails with *"Upstream request
failed: Endpoint is unavailable"* for any request containing a `tools` array,
while plain completions succeed in ~3s, and `nemotron-3-ultra-free` /
`deepseek-v4-flash` handle tools correctly on the same route.

**Why it matters.** The operator asked for Ox Alpha first. Honouring that for
agent roles would fail every agent run. OpenEvolve's mutation calls are plain
completions, so Ox Alpha *is* used where it works — which is the majority of
model traffic.

**Self-correcting.** The doctor probes tool support live and writes the result
into `verified_capabilities`. When upstream fixes it, the router promotes Ox
Alpha with no code change. The Models page states the exclusion reason with the
issue reference, so the operator is never left wondering.

---

## D6 — Free status is three-valued and never "unlimited"

**Decision.** `FreeStatus` is `FREE_LIMITED_TIME | FREE | PAID | UNKNOWN`,
defaulting to `UNKNOWN`.

**Evidence.** OpenCode's docs: *"Ox Alpha Free is a stealth model that's free on
OpenCode for a limited time."*

**Why.** Acceptance criterion 27 forbids presenting it as permanently free. A
two-valued free/paid flag would make an unprobed model read as free. `UNKNOWN`
renders as unknown, and the UI badge for `FREE_LIMITED_TIME` carries the caveat.

---

## D7 — Single SQLite writer

**Decision.** Only the API process writes. Engine and workers emit to NDJSON and
a loopback socket.

**Why.** SQLite handles concurrent readers well and concurrent writers badly.
Rather than fighting it with busy-timeouts and retries across a process pool, no
other process opens the database at all. WAL gives readers concurrency.

---

## D8 — Both a durable log and a live socket

**Decision.** Every event goes to NDJSON; the socket is a best-effort mirror.
Ingest is idempotent on `event_id`.

**Why.** Socket-only loses everything emitted while the control plane restarts.
Log-only adds tailing latency to the live view. Together with idempotent ingest,
the redundancy costs nothing and a control plane that was down backfills the
whole run on restart.

---

## D9 — Drop under pressure, but count it

**Decision.** Bounded queue; overflow increments `dropped_overflow`, surfaced on
the System page and as a status-bar indicator.

**Why.** Blocking would let telemetry throttle evolution, which section 25
forbids. Silent dropping would make the UI lie, which section 36 forbids. The
only remaining option is to drop visibly.

---

## D10 — Checkpoint-on-demand via a control file at a safe boundary

**Decision.** `checkpoint_now` writes a request file; the runner polls it just
after `ProgramDatabase.add` returns in the main process and then calls
`_save_checkpoint`.

**Why.** Upstream checkpoints on a fixed interval with no on-demand trigger.
A signal handler could fire mid-mutation and serialise a half-updated database.
Just after `add` returns in the owning process, the database is in exactly the
state the interval checkpoint writes from.

A file rather than `SIGUSR1` because Windows is a first-class target and has no
`SIGUSR1`. Cost: one `os.path.exists` per added candidate.

---

## D11 — Unsupported controls are disabled with reasons, never faked

**Decision.** `RunManager.CAPABILITIES` reports `(supported, reason)`; the UI
renders unsupported controls struck through with the reason in the tooltip.

Reported unsupported, with cause:

- **pause/resume in place** — OpenEvolve has no resumable in-place pause;
  `SIGSTOP` would leave provider sockets and the worker pool undefined. Use
  graceful stop → resume from checkpoint, which genuinely works.
- **fork from candidate** — no upstream API seeds a run from an arbitrary
  candidate; resume from the checkpoint containing it.
- **retry failed evaluation** — retries are the engine's own policy; the control
  plane cannot re-inject one candidate mid-run.

---

## D12 — Canvas lineage graph, no graph library

**Decision.** Custom canvas renderer instead of React Flow or D3.

**Why.** Section 25 targets tens of thousands of candidates; one DOM node each
stops being viable far earlier. Canvas draws 20k nodes per frame with offscreen
culling. Deterministic layout (x = iteration, y = island band + stable hash of
id) keeps nodes from jumping between frames during a live run — which matters
when the operator is watching one lineage.

---

## D13 — A local provider for offline verification

**Decision.** `scripts/local_provider.py` serves real
`/v1/chat/completions` returning valid SEARCH/REPLACE diffs.

**Why.** Acceptance requires proving a real example runs end-to-end. That must
be demonstrable without a paid key and reproducible in CI. Because it is seeded,
the run is deterministic — better for regression testing than a live model.

**Boundary.** It is a *model provider*, matching the `local-openai-compatible`
profile. The engine, database, MAP-Elites, evaluation, checkpointing and every
event are the real implementations. Only text generation is local.

---

## D14 — Isolation by environment, not by policy

**Decision.** `OpenCodeIsolation.env()` builds a *filtered* environment with
`HOME` and every XDG path redirected into the workspace, dropping inherited
`OPENCODE_*`/`OMO_*` variables entirely rather than overwriting them.

**Why.** A promise not to write to `~/.config/opencode` is only as good as the
code keeping it. A child that cannot see the path cannot touch it, whatever it
tries. `_assert_safe` additionally refuses to create or write any path under an
operator-owned root, and `preflight()` fails closed: if isolation cannot be
established the backend is disabled and native evaluation continues.

---

## D15 — Backfill evaluation status onto candidates

**Decision.** On `candidate.created`, look up any evaluation already recorded
for that id and adopt its status and score.

**Why.** Upstream evaluates a program *before* adding it to the database, so
evaluator events arrive first and their `UPDATE` finds no row. Without the
backfill every evaluated candidate displayed as `pending` forever — visible in
the first live UI verification, where all 28 candidates read PENDING despite 23
successful evaluations. Pinned by
`test_eval_status_backfills_when_evaluation_precedes_candidate`.
