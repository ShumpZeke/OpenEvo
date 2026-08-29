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

---

# OpenEvolve MAX — additional decisions

## D16 — A broker in front of OpenEvolve, not provider logic inside it

**Decision.** OpenEvolve is configured with `api_base:
http://127.0.0.1:8787/v1` and a single model alias. All provider identity,
routing, rate limiting, failover and retry live in the OE-MAX broker.

**Why.** Four things fall out of it that are otherwise hard:

- Upstream stays byte-identical. It already speaks the OpenAI protocol, so this
  needs zero engine changes and the patch surface stays empty.
- Credentials exist in exactly one process. Candidate code and evaluators run
  with no keys in their environment, which is what makes the
  anti-reward-hacking boundary real rather than declared.
- The rate contract is enforced at the single point every request crosses. A
  limiter inside N worker processes is N limiters — and OpenEvolve evaluates in
  a process pool.
- Replacing a stealth-preview model becomes a config edit, which the spec
  requires precisely because Ox Alpha may disappear.

**Cost.** One more process to run, and one more hop of latency — negligible
against a measured 130 s upstream call.

---

## D17 — Ox Alpha re-admitted to tool-using roles

**Decision.** Reversed the earlier build's exclusion of `x-preview-f-free` from
tool-requiring roles.

**Evidence.** `anomalyco/opencode#44300` reported tools requests failing with
"Upstream request failed: Endpoint is unavailable". Re-tested live on
2026-08-26 through `httpx`: tools requests return **HTTP 200**. All five
configured Zen models probe `tools=True`.

**Why it required no code change.** Capability is probed and stored on
`ModelSpec`, never hardcoded. The registry wrote the new truth and routing
followed. This is the payoff for treating capability as measured data — the
same mechanism will exclude it again automatically if the bug returns.

---

## D18 — httpx, chosen by evidence rather than preference

**Decision.** The broker uses `httpx`.

**Evidence.** Probing Zen with Python's `urllib` returned **HTTP 403
`error code: 1010`** for every model — a Cloudflare fingerprint block. `curl`
and `httpx` returned 200 against the same endpoint in the same minute.

**Consequence beyond the broker.** The pre-existing
`control_plane/providers/doctor.py` probes with `urllib`, so it would report a
healthy Ox Alpha as unavailable. Recorded as a known defect in
`project/requirements.md` rather than silently fixed, because it belongs to the
older subsystem and changing it is a separate, testable change.

---

## D19 — Truncation is a distinct outcome, and retries escalate the budget

**Decision.** A 200 carrying `finish_reason=length` is classified `TRUNCATED`,
not success. The router retries it with a **doubled** `max_tokens`, up to a
ceiling, then moves to the next route.

**Evidence.** Ox Alpha spent 7,986–7,997 of an 8,000-token budget on hidden
reasoning. The visible diff was cut off, and **5 of 8 evolution iterations
logged "No valid diffs found"** — roughly 60% of ~130-second requests produced
nothing. After the fix: **0 failures**.

**Why escalate rather than plain-retry.** An identical retry reproduces an
identical truncation. The budget is the variable that has to change.

**Why a ceiling.** Growing forever converts one wasted request into several
larger ones, and on a rate-limited provider that is the worst possible failure
mode.

---

## D20 — Token budget, provider timeout and client timeout are one setting

**Decision.** Zen's provider timeout is 600 s, `max_tokens` is 16,000, and the
engine's client timeout is 900 s — chosen together.

**Evidence.** Fixing truncation by raising `max_tokens` to 16,000 pushed request
duration past the broker's then-180 s timeout: **12 requests, 0 ok, 6 timeouts**.
An earlier attempt at 32,000 tokens ran past OpenEvolve's own client timeout.
Each individual value looked defensible; the combination did not.

Recorded as a decision rather than a config tweak because it will recur with
any reasoning model: raising one of the three alone converts a truncation
failure into a timeout failure and looks like a regression.

---

## D21 — Only NIM gets a rate limiter

**Decision.** NIM uses the full rolling-window `RateLimiter`; Zen and OpenRouter
use `NullLimiter`.

**Why.** The spec is explicit that 48 RPM is the NIM account's contract and must
not be applied to Ox Alpha unless that provider independently requires it.
Applying it everywhere would throttle a provider that has not asked to be
throttled, and would make the NIM bound harder to reason about.

`NullLimiter` is a null object rather than `Optional[RateLimiter]` so no call
site has to special-case "no limit" — the shape where a real limiter eventually
gets skipped by accident.

---

## D22 — The limiter carries an epsilon and a minimum wait

**Decision.** Token comparison uses `>= 1.0 - 1e-9`, and any positive wait is
floored at 1e-4 s.

**Evidence.** Refilling the bucket by exactly the required amount lands on
`0.9999999999`. A strict `>= 1.0` then computed a ~1e-12 wait, and `acquire()`
span forever making invisible progress — the tests **hung** rather than failed,
which is how it was found.

Recorded because it is the kind of bug that looks like a deadlock and is
actually arithmetic.

---

## D23 — SQLite and an append-only event log instead of PostgreSQL

**Decision.** Deviates from the spec's PostgreSQL/pgvector/Parquet/DuckDB stack.

**Why.** The existing control plane already stores full provenance and lineage
in SQLite with an append-only NDJSON event log as the source of truth. At the
current scale — tens of thousands of candidates — SQLite with WAL and proper
indexes is not the bottleneck; a 130-second model call is. Adding a database
server would satisfy the letter of the spec while making the system harder to
run, which §20 explicitly warns against ("do not cargo-cult").

**Migration path.** The event log is the source of truth and projections
rebuild from it, so moving to PostgreSQL later is a new projector rather than a
data migration. Recorded as a deviation in `project/requirements.md`, not
hidden.

---

## D24 — Ablation arms exist as code, not as removals

**Decision.** `UniformRandom` and `EpsilonGreedy` ship alongside
`DiscountedThompsonSampling` behind one `Selector` interface.

**Why.** The spec requires ablations including "no operator bandit". If that
ablation means deleting code, it is expensive to run and easy to get wrong.
As a one-line substitution it is cheap enough to run every time, which is the
difference between an ablation that is specified and one that actually happens.

---

## D25 — Attribution rides on `Program.metadata`, because nothing else crosses

**Decision.** The link from a candidate to the model request that generated it
is stamped onto `Program.metadata` inside the worker, by wrapping
`process_parallel._run_iteration_worker`.

**Evidence.** A ContextVar was the right mechanism for the in-process path and
was measured failing anyway: a live 4-iteration run produced **3 candidates and
0 attributed**. In the default `process_parallel` path the model request happens
in a worker process and `database.add` happens in the main process, which
receives only a pickled `SerializableResult`. Nothing in memory crosses that
boundary — the value was correct in every worker and simply absent on the other
side.

`Program.metadata` is a dataclass field, so it survives `to_dict()` → pickle →
`Program(**dict)`. `_run_iteration_worker` is the only frame that spans both
halves of the problem, which makes it the only place the stamp can be applied.

**Cost, stated honestly.** It is a second attribution channel alongside the
ContextVar, and two mechanisms is worse than one. The ContextVar is kept
because the non-parallel path has no worker frame to wrap. `_attribution_of`
resolves them in one place with metadata winning, so no caller has to know.

**Would change if.** Upstream gave `SerializableResult` a general-purpose
passthrough field, or dropped the process pool.

---

## D26 — Two cases are left unattributed rather than guessed

**Decision.** A migrant copy and a stale context are recorded as *no
attribution*, not as an attribution to the most plausible request.

**Evidence.** `_migrate_programs` copies metadata wholesale into the migrant, so
without an explicit check a single generation would be charged to its route
three times. Measured on the verification run: one generation on island 0 was
copied to islands 1 and 2 with an identical `code_hash`. That is a 3×
inflation of exactly the attempt count that `route_quality` ranks routes on.

This is the no-fake-data rule applied to analysis rather than to the UI. A
plausible attribution is worse than a null one, because a null is visible in
`attribution_coverage` and a wrong one is not.

---

## D27 — The attempt set is model requests, not candidates

**Decision.** `route_quality` charges every mutation-role model request as an
attempt, including ones that produced nothing.

**Evidence.** Ox Alpha was measured at 26% success and a 284 s p50. A route that
burns 292 seconds and returns an unusable diff produces no candidate at all —
counting candidates would erase its worst outcome and make the slowest, least
reliable route look the cleanest.

Failures are counted separately from unparseable responses, because a timeout
is a route problem and an inapplicable diff is a model problem, and the fix for
each is different. Both are still charged, because both consumed the wall-clock
and the rate budget the efficiency measures divide by.

---

## D28 — Record the route that served, keep the alias that was asked for

**Decision.** `model_requests.provider/model` hold the route that actually did
the work; the broker alias survives as `requested_model` in metadata.

**Why.** OpenEvolve is pointed at the broker and only ever names
`oe-max-primary`. Recorded as asked, every route through the broker collapses
into one row called `local/oe-max-primary` — in exactly the configuration this
project ships — and no route comparison is possible.

The broker already stamped `body["oe_max"]` on every response for provenance;
this reads it back rather than inventing a second mechanism. Keeping the alias
matters too: when a route substitution looks wrong, what the engine asked for
is the first thing to check.

**Cost.** The projection now lets the completed event overwrite provider and
model, so event order matters where it did not before. Started events carry the
alias and completed events carry the route; a reordering would show the alias.

---

## D29 — Pinning a route drops failover, not policy

**Decision.** `Router.chat_pinned` applies the same retry and truncation
escalation as the chain, to a single route.

**Evidence.** The broker's pinned path called `provider.chat` directly. Measured
consequence: a 16-token budget made `nemotron-3-ultra-free` return
`finish_reason=length` after spending 17 tokens on hidden reasoning, where the
same call on the chain escalates the budget and succeeds.

Beyond the bug, this is what makes a route A/B trustworthy. Every arm of the
experiment is pinned by definition, so the old behaviour would have compared
routes under a policy the production chain does not use — measuring the policy
difference and reporting it as a difference between models.

---

## D30 — Verification reports, it does not enforce

**Decision.** A candidate that fails V1 verification stays in the population.
The failure is emitted as an event with its counterexample.

**Why.** Everything in `control_plane/` is instrumentation, installed by
wrapping upstream methods at runtime (D1). Instrumentation that silently
deleted the engine's work would make this fork behave differently from upstream
while the patch surface stayed empty — a divergence no test of ours would
catch, because our tests check that events are emitted, not that the population
is the same as upstream's.

**Cost, stated honestly.** A cheating candidate can still become and remain the
champion. The operator sees `candidate.verification.failed` and decides.
Enforcement is a real and reasonable next step; it is a different decision, and
it should be made deliberately rather than arrived at.

---

## D31 — Verify champions and outliers, not everything

**Decision.** V1 runs on a new champion and on an improvement far beyond the
run's own history. Not on every candidate.

**Evidence.** Verification of the shipped example costs ~1 s including 15
randomized trials. At 12 iterations that is negligible; at the throughput
multi-offspring is designed to buy, it is the throughput. Both extremes are
wrong: verifying nothing means the first candidate that games the metric
becomes the champion and every later generation is built on it.

**Why those two.** A champion is what the run now optimises around, so its
being wrong is the most expensive thing that can happen. And a score that
leaps past the run's own distribution of improvements is the exact shape of a
candidate that stopped solving the problem and started reporting a number.

---

## D32 — Median and MAD, not mean and standard deviation

**Decision.** `SuspicionDetector` uses a modified z-score built from the median
and the median absolute deviation.

**Why.** The thing being detected is an outlier. An outlier drags the mean and
inflates the standard deviation, so a conventional z-score test is desensitised
by the very event it exists to catch — the first cheat is flagged, and it makes
the second one look ordinary. The median and MAD are unmoved by a single
extreme value.

A floor on the scale is equally load-bearing and less obvious: late in a run
every improvement is ~0, the MAD collapses toward zero, and any nonzero jump
scores as infinitely unusual. Without the floor the detector flags every
candidate on a plateau, which is where a run spends most of its time.

Flagged jumps are still recorded into the history. Excluding them would make a
genuine breakthrough permanently suspicious — and every real improvement after
it, since the distribution would never learn the new scale.

---

## D33 — Seed the benchmark task's draws rather than averaging more runs

**Decision.** `benchmarks/tasks/fn_min_seeded` fixes one seed per trial, so a
program scores the same number every time. It sits beside
`examples/function_minimization` rather than replacing it, because `examples/`
is upstream and byte-identical.

**Why.** The upstream task's *unchanged seed program* scored 1.0330–1.4188, a
spread of 0.39 — wider than any improvement worth making. A run had already
reported "new best solution found" and finished with a best program
byte-identical to the seed. The alternative, evaluating N times and comparing
distributions, costs N× and still cannot separate two programs whose difference
is smaller than the noise. Fixing the draws also makes the comparison *paired*:
two programs meet the same ten draws, so the draw-to-draw variance leaves the
difference as well.

**Cost, stated honestly.** Scores are no longer comparable to the upstream
task's — this is one particular sample of that metric, not an estimate of its
mean, and both READMEs say so. And `TRIAL_TIMEOUT_S` is wall clock, so a
candidate whose trials land near five seconds can still complete on an idle
machine and time out on a busy one. It stays, because the alternative is an
unbounded loop hanging the run.

**Would change if.** Upstream's evaluator grew a seed parameter. Then this
directory would be a thin config rather than a copy.

---

## D34 — Set `reasoning_effort` in the config as well as the adapter

**Decision.** Both `configs/oe_max/local.yaml` and the broker's local adapter
send `reasoning_effort: "none"`.

**Why.** Belt and braces is usually a smell, but here the two cover different
paths. `api_base` is a config field: pointing it at Ollama's own `/v1` — the
obvious move to take the broker out of the picture — removes the adapter and its
setting together. Without it a local reasoning model answers entirely in
`message.reasoning` and returns an empty `content`: 308.7 s per iteration
producing nothing, against 99.3 s producing an applicable diff, with
`finish_reason: stop` both times so it does not present as truncation.

**Checked for the small-model case**, because the config applies to whatever
model runs and a small model's thinking fits the budget: on `qwen3:0.6b`, n=16
each, 8/16 applicable while thinking against 7/16 without — noise — at 3.87 s
against 0.61 s. Right for both sizes, for different reasons.

**Would change if.** A role wanted thinking. `OE_MAX_LOCAL_REASONING` already
turns it back on, and a judge is the obvious candidate.

---

## D35 — Cache what a subprocess answers, not what a sensor reads

**Decision.** `OpenCodeIsolation.status()` caches per workspace for 30 s.
`gpu_probe()` and `psutil.cpu_percent()` do not cache at all.

**Why.** `/api/system` is polled every five seconds and took 959 ms, of which
874 ms was two subprocesses — `opencode --version` at 553 ms and a
container-runtime probe at 256 ms — re-establishing that OpenCode is still
installed and Docker still is not. Those answers do not change between polls.
Utilisation does, and a cached utilisation figure would be a number nothing
measured, which is the one thing this codebase does not do.

**Cost, stated honestly.** A status can be 30 s stale. `checked_at` is the time
of the underlying check rather than of the call, and the System Health panel now
renders "checked 12s ago" — a cache whose staleness is invisible is a worse
trade than the second it saved. `preflight()` stays uncached because it has side
effects a caller may rely on, and `max_age=0` forces a fresh check.

**Would change if.** The check got cheap. It is two process spawns; nothing
about that is going to change.
