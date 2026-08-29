# Gotchas

Defects that produced no error message, or the wrong one. Each cost real time to
find, and each is the kind that looks like something else — a slow provider, a
flaky test, a model that "just isn't very good".

If you are debugging something here and it does not make sense yet, read this
first. They are ordered roughly by how often they come up, not by severity.

---

These cost real time to find. Each is a trap you would otherwise fall into.

## `urllib` is Cloudflare-blocked; `httpx` is not

Probing Zen with Python's `urllib` returns **HTTP 403 `error code: 1010`** for
every model. `curl` and `httpx` return 200 against the same endpoint in the same
minute. It is a client-fingerprint block, not a provider outage.

**Consequence, now fixed:** `control_plane/providers/doctor.py` used to probe
with `urllib` and so reported a healthy Ox Alpha as unavailable. It now uses
`httpx`, keeps urllib only as a last resort, and reports a 1010 as an
*inconclusive transport block* rather than a provider fault — blaming the
provider for our own client's fingerprint would be worse than admitting we
could not tell.

Keep the trap in mind anyway: any new probing code you add must not reach for
`urllib`.

## A reasoning model can spend its entire token budget on hidden reasoning

Measured on Ox Alpha, the former primary: **7,986–7,997 reasoning tokens out of
an 8,000 budget.** The visible diff gets truncated and OpenEvolve logs `No valid
diffs found` — five of eight iterations produced nothing from ~130-second
requests.

**The trap outlived that model.** Its replacement reasons too:
`nemotron-3-ultra-free` spent 39 completion tokens thinking about a two-word
answer, and `nemotron-3.5-lightning-free` truncated at 64 of 64 tokens, all of
them reasoning. Only `laguna-s-2.1-free` reports zero — which is exactly why it
leads the judge and fast chains rather than the reasoning ones.

Handled: the broker classifies `finish_reason=length` as `TRUNCATED` and retries
with a **doubled** budget (an identical retry reproduces an identical
truncation). Config `max_tokens` is 16,000.

**If you change `max_tokens`, you must change two other things with it** — see
3.3.

## Token budget, provider timeout and client timeout are ONE setting

Raising `max_tokens` to 16,000 pushed requests past the broker's then-180s
provider timeout: **12 requests, 0 ok, 6 timeouts.** An earlier try at 32,000
ran past OpenEvolve's own client timeout instead.

Current coupled values:

| Setting | Value | Where |
|---|---|---|
| `max_tokens` | 16,000 | `configs/oe_max/evolution.yaml` |
| provider timeout | 600 s | `oe_max/providers/registry.py` (Zen) |
| client timeout | 900 s | `configs/oe_max/evolution.yaml` |

Change one alone and a truncation failure becomes a timeout failure that looks
like a regression.

## A listed model is not a working model

Zen's `/models` lists `deepseek-v4-flash-free`; calling it returns HTTP 400
"Model is unavailable" — still true on 2026-08-26, a year-long liar.
`mimo-v2.5-free` returns 429 `FreeUsageLimitError`, and the newly-listed
`muse-spark-1.2-contributor-free` returns HTTP 500. Three of Zen's eight listed
free models do not serve.

Discovery is therefore two-stage — list, then smoke-test — in
`oe_max/providers/registry.py`. Do not "simplify" it back to one stage.

And read the trap above for the converse, which is the half that was missing: an
*unlisted* model is not a model either.

## The event bus must be rebuilt after `fork()`

OpenEvolve evaluates in a `ProcessPoolExecutor`. A forked child inherits the
`EventBus` object but **not** its worker thread, so it would queue events into a
buffer nothing drains. Both the bus and the instrumentation are keyed by owning
PID. Symptom if broken: `model_requests: 0` despite a run clearly making model
calls. Pinned by `test_bus_is_rebuilt_after_fork`.

## Under `spawn`, the worker initializer resolves to upstream's, not ours

Same symptom as 3.5, different cause, and the one that actually bites on
Windows and macOS.

`openevolve/process_parallel.py` hands the pool a module-level reference:
`"initializer": _worker_init`. `install_worker_hook` rebinds that attribute to a
wrapper that installs telemetry in the child.

Under `fork` the child inherits the parent's patched module, so the wrapper is
there. Under `spawn` the initializer is pickled **by reference** and re-resolved
by importing `process_parallel` fresh in the child — which returns upstream's
*original* function and drops the wrapper silently. On Python 3.11+ upstream
does not set `mp_context`, so the context is the platform default: `fork` on
Linux, **`spawn` on Windows and macOS**.

Nothing errors. Candidates still arrive, because they travel back on the
returned Program objects (3.7), so the only symptom is that `model_requests`,
`tokens` and `iterations_done` all read **0** on a run that is plainly working.
That is the plausible-looking zero the no-fake-data rule exists to prevent, and
it is why every measurement in this repo taken on Linux was real and the same
run on Windows looked idle.

Measured on one 12-iteration local run, before and after:

| | before | after |
|---|---|---|
| PIDs emitting telemetry | 1 | 3 |
| `model.request.started` | 0 | 11 |
| `generation.completed` | 0 | 9 |
| `evaluator.started` | 1 | 12 |
| `iterations_done` / `model_requests` | 0 / 0 | 11 / 11 |

Fixed by routing the initializer through `_worker_bootstrap` in
`control_plane/telemetry/instrument.py` — a function in *our* package, which the
child therefore resolves to the real thing — installed by
`install_pool_initializer_hook`, which wraps the stdlib `ProcessPoolExecutor`
constructor rather than upstream's call site so that `openevolve/` stays
byte-identical. The substitution only happens when `EVOLUTION_TELEMETRY` is set,
so the plain upstream CLI is untouched. Pinned by
`tests/evolution/test_spawn_worker_telemetry.py`.

If you ever see a run producing candidates with zero model requests, check the
emitting PID count in `events.ndjson` before anything else.

## RLIMIT_NPROC counts the user, and a too-tight one fakes a passing test

`processes: 64` reads as "the candidate may create 64". The kernel applies
RLIMIT_NPROC **per UID**, counting everything that user already has, so on a
machine already past 64 the candidate cannot create one thread.

What makes this worth a trap entry is not the failure, it is the *shape* of it.
The evaluator's own worker thread hits the wall first, so it reports `can't
start new thread`, returns zeroed metrics and exits 0. The sandbox then reports
`ok`. Measured on CI: the honest seed program "evaluated normally" with
combined_score 0.0, and the memory-bomb and infinite-loop tests both reported
their candidate as **not stopped** — because it had never run.

A limit that silently prevents the evaluation, and thereby makes a containment
test pass, is worse than no limit. `ResourceLimits.processes` is now an
allowance above current usage rather than an absolute ceiling.

## A sandboxed candidate cannot import numpy without pinning BLAS threads

`RLIMIT_NPROC` is what stops a candidate fork-bombing the host. OpenBLAS, MKL
and OpenMP size their thread pools from the **machine's** core count at import
time, ignoring what the process is allowed, so on a many-core host the very
first `import numpy` inside the sandbox dies with
`OpenBLAS blas_thread_init: pthread_create failed` — and the candidate is
recorded as crashed before running a line of its own code.

Fixed by pinning `OMP_NUM_THREADS` and the four related variables to `1` in
`ResourceLimits.child_env()`. The ceiling is the security property and does not
move; the thread pools give way. Single-threaded BLAS also makes a score
reproducible rather than dependent on how many cores were free, so this is the
right default independently of the crash.

Measured on a 16-core CI runner. It cannot reproduce on Windows, where the
POSIX-limits tests skip entirely — which is the same reason the trap above went unseen.

## The example task's score is noisier than most improvements

`examples/function_minimization` evaluates a *stochastic* program: the seed uses
`np.random.uniform` and neither it nor the evaluator fixes a seed. The config's
`random_seed` governs the evolution process, not the candidate's execution.

So the same unmodified program does not score the same twice. Measured, five
evaluations of the untouched seed:

    1.4184   1.4431   1.4060   1.4229   1.0494        range 0.3937

Three consequences, all of which have caught someone here.

**"New best solution found" fires on noise.** A 12-iteration run reported a new
best at iteration 9 and finished at 1.4209 — and its best program was
byte-identical to the seed. Nothing had been improved; the same code drew a
luckier sample.

**Single-run score comparisons between models or configs are worthless** unless
the difference exceeds ~0.4, which is larger than most real improvements. Two
runs finishing at 1.4198 and 1.4209 have told you nothing.

**A mutation can be accepted for being lucky rather than better**, which means
even "the best program changed" is weak evidence on its own.

**Use `benchmarks/tasks/fn_min_seeded` instead.** It is the same task with
the draws pinned, so the same program scores 1.406051 every time — measured
over twenty evaluations, spread exactly 0.0. `examples/` is upstream and
byte-identical, which is why the deterministic evaluator lives beside it rather
than replacing it.

Everything above still applies to `examples/function_minimization` itself, and
scores from the two are **not** interchangeable: fixing the draws makes the
seeded one a particular sample of the upstream metric, not an estimate of its
mean. A score quoted from one run of the upstream task is evidence that the
machinery ran, not that the search worked.

## A metric that averages can prefer the worse program

Found the first time the seeded task was used for a real comparison, and it is
a property of upstream's metric rather than of the seeding.

`combined_score` averages the distance-to-optimum over ten trials. A
local-refinement variant lands about five times closer than the seed program on
**9 of 10 seeds** — and scores *lower*, 1.3564 against 1.4061. On the tenth it
refines into the wrong basin at distance 3.99, and that single trial moves the
mean from 0.19 to 0.43.

So evolution on this task will reject a program that is excellent nine times in
ten in favour of one that is mediocre throughout. If a promising-looking
mutation is being rejected, check the per-seed record before concluding the
model produced something bad:

```bash
python benchmarks/tasks/fn_min_seeded/compare.py A.py B.py
```

It prints the ten trials behind the aggregate and says so explicitly when the
majority winner is the lower scorer. The weights are deliberately left as
upstream's — changing them would make every number already recorded
incomparable.

## A score rise on the benchmark task may just be a bigger budget

Neither `examples/function_minimization` nor `benchmarks/tasks/fn_min_seeded`
scores runtime, so raising the candidate's own search budget raises the score
for free: 1.4061 at `iterations=1000`, 1.4513 at 2000, 1.4805 at 20000.

The first real run against the seeded task found precisely that and nothing
else -- 30 iterations of `qwen3:0.6b` in about three minutes, whose one accepted
change was `iterations=1000` -> `iterations=2000`. The improvement is real and
reproducible; it is also the cheapest move on the board.

Before reporting that a model improved a program, read the diff. `average_seconds`
in the artifacts moves when a candidate has bought score with sampling.

## Replacing a `np.random` factory with a function breaks `default_rng`

Only bites code that pins randomness by monkeypatching, which
`benchmarks/tasks/fn_min_seeded/evaluator.py` does — but it fails in a way that
gives no hint of the cause:

    TypeError: isinstance() arg 2 must be a type, a tuple of types, or a union

NumPy's `default_rng` resolves `RandomState` out of the `np.random` namespace
*at call time* and hands it to `isinstance`. Replace that name with a plain
function and `np.random.default_rng(7)` — an ordinary, correctly-seeded call
that has nothing to do with the patch — raises from inside NumPy. Measured on
NumPy 2.4.6.

The stand-in has to be a real type, and has to answer `isinstance` and
`issubclass` by delegating to the original, or the patch silently changes
behaviour for anything that passes a real instance. `_PinnedMeta` in that
evaluator is the pattern; `test_patched_types_answer_isinstance_like_the_real_ones`
fails if it regresses.

## A model paraphrases the code it is quoting, and the diff matches nothing

A SEARCH/REPLACE diff only applies if the SEARCH block is byte-identical to the
file. Ask a model for a diff without saying so and it will often *tidy* the code
it quotes — re-indent it, normalise quotes, drop a blank line, reword a comment.
The result reads like a correct diff and matches nothing.

Nothing reports an error. The request succeeded, the model answered, the block
is well-formed. It simply fails to apply, and the iteration is scored as a
mutation that produced no improvement rather than as a mutation that never
happened.

Measured on a local Qwen3.5-27B, two prompts for the same task:

| `system_message` | mean tokens | applicable |
|---|---|---|
| plain instruction | 1396 | **1 of 4** |
| explicit output rules | 400 | **4 of 4** |

The unconstrained arm hit its `max_tokens` ceiling in three of four samples: it
rambles, gets truncated mid-diff, and a truncated SEARCH block matches nothing.
Raising the ceiling does not help — it buys a longer ramble at ~0.3 s per token.

The lines that fix it forbid prose around the diff and demand the **smallest
unique** SEARCH span, copied exactly with no paraphrasing or re-indentation.
They cost nothing: the constrained prompt was also 3.5× shorter and 3.3× faster.
Per *usable* diff the difference is about 13×.

Two things follow. Optimising a local prompt for brevity can make it strictly
worse, so measure applicable diffs rather than tokens per second. And when a
local run shows a healthy request rate and no improvements, check whether the
diffs are applying before assuming the model is weak.

## `--verify` loads every model, which a local box cannot afford

Verification smoke-tests every configured model with a real completion, twice —
once plain and once with a tools payload. Against remote providers that is
seconds. Against local ones it means **loading each model in turn**, and a
16 GB model on a 16 GB machine cannot be loaded alongside another.

Measured: five discovered local routes (three Ollama tags, two LM Studio) took
`--verify` past ten minutes and drove free RAM to **0.3 GB**, with the machine
paging throughout. Nothing was broken; it was doing exactly what it was asked,
sequentially, with a working set larger than the machine.

The probe budget is now provider-aware — 16 tokens locally instead of 200,
since `reachable` is decided by HTTP 200 and not by what the model said — but
that treats the smaller half of the cost. The load time is inherent.

**What to do:** do not pass `--verify` when several large local models are
configured. Startup discovery, which is the default, only reads `/v1/models`
and is instant; it is enough to route. Verify one model deliberately if you
need to know whether it serves, rather than sweeping all of them.

Worth remembering that "listed but broken" — the failure two-stage discovery
exists to catch — is a cloud-provider problem. A local model you pulled
yourself and can see in `ollama list` is a much weaker candidate for it.

## The limiter needs its epsilon

Refilling the token bucket by exactly the required amount lands on
`0.9999999999`; a strict `>= 1.0` computes a ~1e-12 wait and `acquire()` spins
forever. The tests **hang** rather than fail. `_EPS` and `_MIN_WAIT` in
`oe_max/limiter.py` exist for this. Do not remove them.

## Nothing in memory crosses the worker process boundary

In the default `process_parallel` path the model request is made in a **worker
process** and `ProgramDatabase.add` runs in the **main** process, which receives
only a pickled `SerializableResult`. A ContextVar — or a thread-local, or a
global — is correct inside the worker and simply does not exist on the other
side.

This cost a full debugging cycle to see: attribution was set correctly in every
worker and a live 4-iteration run still produced **3 candidates and 0
attributed**. Nothing was broken; the value was being dropped at the process
edge.

`Program.metadata` is the one channel that crosses, because it is a dataclass
field that survives `to_dict()` → pickle → `Program(**dict)`. So
`process_parallel._run_iteration_worker` is wrapped — it is the only frame
spanning both halves — and stamps the attribution onto the child program's
metadata before returning (`ATTRIBUTION_KEY` in
`control_plane/telemetry/instrument.py`).

**If you need to get anything else from a worker to the main process, that is
the channel.** And check `migrant`: `_migrate_programs` copies metadata
wholesale into the copy, so anything you put there will be duplicated onto
migrants unless you exclude them.

## Through the broker, every route is called `oe-max-primary`

OpenEvolve is pointed at the broker and only ever names the alias. Recorded
naively, every route collapses into a single row called `local/oe-max-primary`
— in exactly the configuration this project ships — and no per-route analysis
is possible at all.

The broker already stamps `body["oe_max"]` with the serving provider, model,
attempt and reasoning tokens. The instrumentation reads it back off the parsed
response (the OpenAI client keeps unknown fields in `model_extra`) and the
completed event wins the projection, so `model_requests.provider/model` is the
route that did the work and the alias survives as `requested_model`.

If you add a new endpoint that fronts other providers, stamp it the same way or
its traffic becomes unanalysable.

## Pinning a model must not drop the retry policy

`_resolve_pinned` in the broker used to call `provider.chat` directly, skipping
retry and truncation escalation. A pinned reasoning model then truncates where
the same model on the chain succeeds — measured: a 16-token budget made
`nemotron-3-ultra-free` return `finish_reason=length` after spending 17 tokens
on hidden reasoning.

It now goes through `Router.chat_pinned`, which applies the same policy without
failover. This matters beyond correctness: every arm of a route A/B is pinned,
so the old behaviour would have made the experiment measure the policy
difference instead of the models.

## A circuit breaker is inert when its window is shorter than the latency

Ox Alpha was measured at **8% success over 12 attempts** with the breaker
sitting closed on one recent failure, and the reason is not what it looks like.
The breaker trips on N failures inside a rolling **60-second** window. Ox
Alpha's requests take ~300 seconds, so every failure ages out before the next
arrives and the count never reaches the threshold.

So the breaker cannot protect the slowest routes — exactly the ones where a
wasted request costs most. `RouteHealth.degraded()` covers that gap with a
window that counts *attempts* rather than seconds: below 25% success over at
least 10 attempts, a route is demoted out of the chain, and re-admitted as soon
as its recent attempts recover (the window is the memory, so no cooldown).

Two properties worth preserving if you touch it:

- **If every route is degraded, the least bad still serves.** A run that dies
  with "no usable route" is worse than one served slowly by a flaky provider.
  The route table says `serving anyway` when that happens.
- **Pinning still reaches a degraded route.** Demotion is a chain-selection
  policy; silently redirecting a pinned request would make an A/B experiment
  measure something other than what it named.

## The plain CLI installs no telemetry, and every feature rides on it

`scripts/run-evolution.sh` exec'd `openevolve-run.py`, the untouched upstream
CLI. Upstream installs no instrumentation, and **every OE-MAX feature is
installed by that instrumentation** — operator steering, attribution,
multi-offspring, verification, sandboxed evaluation, the bandit. So:

```bash
OE_MAX_OPERATORS=1 ./scripts/run-evolution.sh --iterations 12
```

set an environment variable that nothing ever read. The run succeeded, the
score improved, and not one of those features happened. No error, no warning,
no event log — the only symptom was the absence of something you had no reason
to check for.

`auto_install_from_env()` is called from exactly one place,
`control_plane/runner/entrypoint.py`, and it needs both `EVOLUTION_TELEMETRY`
and `EVOLUTION_RUN_ID`.

**Scope, checked rather than assumed.** The Control Center and both experiment
harnesses start runs with `POST /api/control/runs`, which goes through
`RunManager` → that entrypoint, so they were always instrumented. Only the
standalone shell script was not. Measurements recorded from `ablation.sh` and
`route-experiment.sh` therefore stand; a measurement someone took by running
the shell command in §4b/§4c/§4f by hand does not.

Fixed: both launchers now exec the entrypoint and generate a run id when the
caller has not supplied one. `tests/evolution/test_launch_scripts.py` pins it.

**If you add a feature that hangs off instrumentation, check which launcher
path the operator will actually use.** A flag that silently does nothing is
worse than one that errors.

## A configured model can simply stop existing

This is the trap that cost the most, because nothing failed loudly.

`x-preview-f-free` (Ox Alpha) was the configured primary in both routing
layers. On 2026-08-26 it was gone from OpenCode Zen: absent from `/models`, and
answering `ModelError: Model x-preview-f-free is not supported`. Note that this
arrives as **HTTP 401**, so it reads like a credential problem at a glance —
the body is what distinguishes it, since a paid Zen model returns `AuthError:
Missing API key` instead.

Two NIM fallbacks turned out never to have existed at all:
`deepseek-ai/deepseek-v4-pro` and `qwen/qwen2.5-coder-32b-instruct` are not in
NVIDIA's catalogue, and NIM hosts no Qwen model. The "strong fallback" could
not have served a single request.

**What was missing was the converse of an existing check.** Two-stage discovery
asked "is a listed model serveable?" and never asked "is a configured model
still listed?". `Registry.reconcile()` now asks it on every discovery and
disables what is no longer there. Listings are unauthenticated on both Zen and
NIM, so this costs one GET and works before any key exists — run against the
real endpoints it rediscovered all three findings above on its own.

The general shape: **a model id written from memory is a claim that decays.**
The catalogue (`configs/oe_max/providers.yaml`) therefore names patterns and
materialises concrete ids from each provider's own listing.

## An exhausted free allowance is not a rate limit

Both are HTTP 429, and Zen's free-limit body even says *"Rate limit exceeded.
Please try again later."* Only the error **type** (`FreeUsageLimitError`)
distinguishes them.

Retrying cannot refill a pool, so treating them alike spent the entire retry
budget collecting the same error four times before failing over.
`Outcome.FREE_LIMIT_EXHAUSTED` is not retryable and parks the route for 15
minutes (`RouteHealth.park`). It deliberately does **not** trip the circuit:
the provider is up, our allowance is gone, and an outage cooldown would just
re-probe into the same refusal.

If you add health reporting, remember a parked route's breaker reads
`closed, 0s remaining` — reporting that says the route is fine while it is
being skipped.

---
