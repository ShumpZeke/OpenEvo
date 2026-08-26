# Handoff — read this first

You are picking up **Evolution / OpenEvolve MAX**. This document is written for
whoever continues the work, human or model. It says what is true right now, what
is verified versus merely written, where the traps are, and what to do next in
priority order.

Everything below was measured on this machine unless marked otherwise. Where a
thing is unverified, it says so — do not upgrade a "probably" into a "works".

---

## 1. What this repository is

A production fork of OpenEvolve with two layers built *around* the engine, not
into it:

```
                    ┌──────────────────────────────────┐
                    │  Browser Control Center (19 views)│
                    └────────────────┬─────────────────┘
                                     │  REST + SSE
                    ┌────────────────┴─────────────────┐
                    │  control_plane/  telemetry,      │
                    │  storage, query/control API      │
                    └────────────────┬─────────────────┘
                                     │  spawns + observes
                    ┌────────────────┴─────────────────┐
                    │  openevolve/  (BYTE-IDENTICAL)   │
                    └────────────────┬─────────────────┘
                                     │  OpenAI protocol
                    ┌────────────────┴─────────────────┐
                    │  oe_max/  broker on :8787        │
                    │  routing · rate contract · retry │
                    │  failover · owns all credentials │
                    └────────────────┬─────────────────┘
                                     ▼
                    OpenCode Zen · OpenRouter · NVIDIA NIM
```

**The engine is byte-identical to upstream.** `diff -rq` proves it, and 437
upstream tests pass. Telemetry is installed by wrapping public methods at
runtime, so the patch surface is empty and upstream merges fast-forward. Keep
it that way — see `PATCH_SURFACE.md` before you consider editing anything under
`openevolve/`.

---

## 2. Run it in three commands

```bash
./bootstrap.sh                     # env, deps, storage, UI build, checks
./scripts/start-broker.sh          # terminal 1 → :8787
./scripts/run-evolution.sh --task function_minimization --iterations 10
```

Watch it:

```bash
./scripts/dashboard.sh             # terminal 3: providers, rate window, routes
./run.sh                           # browser Control Center → :8000
./scripts/verify-providers.sh      # what is actually reachable right now
```

**No API key needed.** OpenCode Zen serves `x-preview-f-free` (Ox Alpha) without
one. That was verified live, repeatedly.

Tests: `./test.sh` → 437 upstream + 233 control plane + 172 OE-MAX.

---

## 3. Traps — read before debugging anything

These cost real time to find. Each is a trap you would otherwise fall into.

### 3.1 `urllib` is Cloudflare-blocked; `httpx` is not

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

### 3.2 Ox Alpha spends its entire token budget on hidden reasoning

Measured: **7,986–7,997 reasoning tokens out of an 8,000 budget.** The visible
diff gets truncated and OpenEvolve logs `No valid diffs found` — five of eight
iterations produced nothing from ~130-second requests.

Handled: the broker classifies `finish_reason=length` as `TRUNCATED` and retries
with a **doubled** budget (an identical retry reproduces an identical
truncation). Config `max_tokens` is 16,000.

**If you change `max_tokens`, you must change two other things with it** — see
3.3.

### 3.3 Token budget, provider timeout and client timeout are ONE setting

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

### 3.4 A listed model is not a working model

Zen's `/models` lists `deepseek-v4-flash-free`; calling it returns HTTP 400
"Model is unavailable". `mimo-v2.5-free` returns 429 `FreeUsageLimitError`.
Discovery is therefore two-stage — list, then smoke-test — in
`oe_max/providers/registry.py`. Do not "simplify" it back to one stage.

### 3.5 The event bus must be rebuilt after `fork()`

OpenEvolve evaluates in a `ProcessPoolExecutor`. A forked child inherits the
`EventBus` object but **not** its worker thread, so it would queue events into a
buffer nothing drains. Both the bus and the instrumentation are keyed by owning
PID. Symptom if broken: `model_requests: 0` despite a run clearly making model
calls. Pinned by `test_bus_is_rebuilt_after_fork`.

### 3.6 The limiter needs its epsilon

Refilling the token bucket by exactly the required amount lands on
`0.9999999999`; a strict `>= 1.0` computes a ~1e-12 wait and `acquire()` spins
forever. The tests **hang** rather than fail. `_EPS` and `_MIN_WAIT` in
`oe_max/limiter.py` exist for this. Do not remove them.

### 3.7 Nothing in memory crosses the worker process boundary

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

### 3.8 Through the broker, every route is called `oe-max-primary`

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

### 3.9 Pinning a model must not drop the retry policy

`_resolve_pinned` in the broker used to call `provider.chat` directly, skipping
retry and truncation escalation. A pinned reasoning model then truncates where
the same model on the chain succeeds — measured: a 16-token budget made
`nemotron-3-ultra-free` return `finish_reason=length` after spending 17 tokens
on hidden reasoning.

It now goes through `Router.chat_pinned`, which applies the same policy without
failover. This matters beyond correctness: every arm of a route A/B is pinned,
so the old behaviour would have made the experiment measure the policy
difference instead of the models.

---

## 4. Live measurements — what the providers actually do

From a real 10-iteration run through the broker, 2026-08-26:

| Route | Requests | Success | Avg latency | Errors |
|---|---|---|---|---|
| `x-preview-f-free` (Ox Alpha) | 15 | **40%** | 220 s | 6 transport, 1 unavailable, 1 server, 1 truncated |
| `nemotron-3-ultra-free` | 1 | **100%** | 112 s | none |

Two things to take from this:

1. **Ox Alpha is slow and unreliable under sustained load.** 40% success, 220s
   average. It is still the operator's chosen primary and the spec's requirement,
   and the retry/failover path is what makes it usable.
2. **The failover chain works in production, not just in tests.** When Ox Alpha
   degraded, the router moved to `nemotron-3-ultra-free`, which returned 100%
   success at half the latency.

That second point is the most interesting open question in the project — see
next steps.

### Free Zen models, probed live

| model | serves | tools | latency |
|---|---|---|---|
| `x-preview-f-free` (Ox Alpha) | yes | yes | 1,969 ms |
| `nemotron-3-ultra-free` | yes | yes | 829 ms |
| `nemotron-3.5-lightning-free` | yes | yes | 1,271 ms |
| `laguna-s-2.1-free` | yes | yes | 1,855 ms |
| `hy3-free` | yes | yes | 2,444 ms |
| `deepseek-v4-flash-free` | **no** | — | 400 "Model is unavailable" |
| `mimo-v2.5-free` | **no** | — | 429 `FreeUsageLimitError` |

`anomalyco/opencode#44300` (Ox Alpha failing on `tools`) **is resolved** — tools
requests now return 200.

Be precise about what that did and did not require, because it is easy to
overclaim: the **capability filter** self-corrects in both directions with no
code change — if the bug returns, the next probe records `supports_tools=False`
and Ox Alpha drops out of tool roles automatically. The **chain order** is a
stated preference and does *not* self-correct; leading tool roles with Ox Alpha
again was a deliberate edit once the evidence changed.

---

## 4b. Operator-labelled mutations (opt-in)

Upstream issues one undifferentiated "improve this program" request. With
`OE_MAX_OPERATORS=1` set for the run, each mutation is asked for as a named
class from the OE-MAX taxonomy, and the label rides through to
`candidates.gen_operator` and the per-operator breakdown.

```bash
OE_MAX_OPERATORS=1 ./scripts/run-evolution.sh --iterations 12
```

Verified on a 12-iteration run: 12 of 13 candidates labelled (the 13th is the
seed), 10 distinct operators asked for, per-operator quality populated.

Three things to know before changing it:

- **It is off by default on purpose.** It changes what the model is asked, so
  turning it on globally would confound the stock-vs-MAX comparison and every
  measurement already recorded.
- **The bandit is not driving it.** Selection is uniform random, seeded from
  `(run_id, iteration)` so a rerun is comparable. The bandit exists and is
  tested, but it learns from per-operator reward and there was none until this
  existed. Measure first; then close the loop.
- **The evaluator's prompt is never steered.** Upstream builds a second
  `PromptSampler` for LLM feedback and marks it with `set_templates()`.
  Steering that one corrupts the *score* rather than the candidate, which is
  much harder to notice than a broken diff.

## 4c. Multi-offspring per request (opt-in)

`OE_MAX_MULTI_OFFSPRING=3` asks each request for N alternatives and turns the
extras into ordinary candidates. Local run: raw yield 1.00 → 2.42 per request,
but **distinct** yield only 0.58 → 0.75 — read the distinct row, not the raw
one.

```bash
OE_MAX_MULTI_OFFSPRING=3 ./scripts/run-evolution.sh --iterations 12
```

The design constraint worth understanding before changing anything:
**prompting alone cannot do this.** Upstream applies *every* diff block it
finds, in sequence, so three alternatives in one response produce one
incoherent merge, not three children. The alternatives are therefore separated
before upstream's parser sees them — `extract_diffs`/`apply_diff` are wrapped so
the primary child is byte-for-byte what it would have been at N=1, and the rest
become siblings in the worker.

Two traps already paid for, both of which failed *silently*:

- **The preamble is not alternative 1.** "Here are three approaches…" contains
  no diff, and applying a diff-free string returns the parent unchanged — the
  run looks healthy and evolves nothing.
- **`apply_diff` calls `extract_diffs` internally.** The wrapper re-enters with
  an already-split alternative that has no marker, and an unconditional
  assignment wipes the stash. The first live run produced zero siblings and no
  error at all.

What is *not* measured: whether a real model's alternatives are actually
different. The local provider has a fixed pool of five mutations. The duplicate
rate is the number that decides this feature — see NEXT_TASKS T2.

---

## 5. What to do next, in priority order

Ordered by expected value, with the reasoning attached so you can disagree with
the ordering rather than guess at it.

### 1. Fast-model routing experiment — highest value

The measurement in §4 makes this urgent: Ox Alpha is 40% reliable at 220 s;
`nemotron-3-ultra-free` was 100% at 112 s. If a cheaper route produces
comparable mutation quality, throughput roughly doubles for free.

**Do not just switch the default.** The operator explicitly wants Ox Alpha
primary. Measure per-operator-class quality first, using the statistics
`router.stats_by_route()` already collects, then propose the change with
evidence.

### 2. Multi-offspring (spec §7F)

At ~220 s and ~8,000 tokens per request, getting 2–3 diverse candidates from one
request is close to a linear throughput win. The latency measurement is what
makes this the second-highest-value item.

Start in `oe_max/search/`, ask for N labelled alternatives in one prompt, split
them, and feed each through the existing G0/G1 gates. Benchmark before enabling
by default — the spec is explicit about that.

### 3. Stock vs MAX benchmark (spec §16)

The harness is ready: `./scripts/run-evolution.sh --profile stock|max`. Both
arms use the **same model** and differ only in whether they go through the
broker. Needs ≥5 seeds. Measure area under the best-so-far curve against
*requests*, not wall-clock.

Expect the baseline to be unusually penalised on this route because it lacks
truncation escalation. That is a real difference but it is provider handling,
not search quality — separate the two when you write it up.

### 4. Sandbox executors

`control_plane/sandbox/opencode.py` enforces the isolation boundary and is
tested (9 tests, including that writes into operator-owned paths are refused).
What is missing is the thing that *runs candidates inside it* — container and
worktree backends with CPU/RAM/pids limits and a wall timeout.

Docker is available on this machine. This is the largest remaining subsystem and
the one that unlocks the anti-reward-hacking work (spec §9).

### 5. Verification stages V1/V2 (spec §8)

Property, metamorphic, differential and hidden tests, then symbolic/SMT and the
independent critic. Depends on the sandbox for anything untrusted.

---

## 6. Blocked, and exactly how to unblock

**NVIDIA NIM and OpenRouter are UNVERIFIED.** No `NVIDIA_API_KEY` or
`OPENROUTER_API_KEY` was available. The adapters, the global rate limiter, the
retry/circuit-breaker path and the routing logic are all implemented and tested
offline against a scripted provider — but the HTTP round-trip to those two
endpoints has never run.

To unblock:

```bash
cp .env.example .env      # add OPENROUTER_API_KEY and/or NVIDIA_API_KEY
./scripts/start-broker.sh
./scripts/verify-providers.sh
```

NIM's model list is deliberately **empty** in `registry.py` — it must be
discovered live, never populated from remembered IDs. The spec is explicit and
the `deepseek-v4-flash-free` finding shows why.

The NIM limiter enforces ≤44 attempt-starts per rolling 60 s. That invariant is
proven by 17 property tests on a virtual clock, but it has never run against the
real NIM endpoint. Watch the dashboard's rolling-window gauge on the first real
NIM run.

---

## 7. Ground rules to preserve

These are not style preferences; each is load-bearing.

1. **Never edit `openevolve/`.** The empty patch surface is what makes upstream
   merges free. Wrap at runtime instead — `control_plane/telemetry/instrument.py`
   shows how.
2. **No fake data in the UI, ever.** If the backend has no value, render "no
   data". There are no fixtures anywhere in `web/` and it must stay that way.
3. **Unsupported controls are disabled with the backend's reason**, never shown
   as buttons that do nothing. `RunManager.CAPABILITIES` is the mechanism.
4. **Never claim a live test passed if it did not run.** Everything unverified in
   this repo is labelled unverified. Keep that discipline — it is the difference
   between documentation and marketing.
5. **Credentials stay in the broker process.** Candidates and evaluators get no
   keys. Redaction runs before persistence, not at render time.
6. **Model and provider IDs are configuration.** Ox Alpha is a stealth preview
   and may vanish; nothing should need a rewrite when it does.

---

## 8. Where everything lives

| Document | What it answers |
|---|---|
| `README.md` | What this is, how to run it |
| `ARCHITECTURE.md` | How the pieces fit and why |
| `DECISIONS.md` | 24 decisions with evidence and what would change them |
| `BUILD_LOG.md` | What was built, in order, and what measurements changed |
| `BENCHMARKS.md` | Live numbers from the real primary route |
| `REQUIREMENTS_PROGRESS.md` | Every spec requirement → status, gaps, blockers |
| `FEATURE_COVERAGE_MATRIX.md` | Acceptance criteria scored honestly |
| `PATCH_SURFACE.md` | Every upstream file touched (none) |
| `UPSTREAM_SYNC_STRATEGY.md` | How to merge a future OpenEvolve release |
| `TELEMETRY.md` · `PROVIDERS.md` · `SANDBOX.md` · `SECURITY.md` | Subsystem detail |
| `TEST_STRATEGY.md` | What is tested and, importantly, what is not |
| `.handoff/` | The original source specs (inputs, not project source) |

The original build specs are preserved under `.handoff/` — read
`.handoff/MAX/OpenEvolve_MAX_OX_Alpha_Build_Pack/` for the full requirements if
you need the authoritative wording.

---

## 9. Current state, precisely

```
branch    main  (and claude/unzip-goals-instructions-vz9ely — identical)
tests     437 upstream + 233 control plane + 172 OE-MAX = 842 passing
engine    openevolve 411fb59c (v0.3.2), byte-identical, Apache-2.0
verified  OpenCode Zen / Ox Alpha — live, keyless, end-to-end evolution
unverified NVIDIA NIM, OpenRouter — no credentials
```

Since the last handoff, four things that were structurally impossible are now
measurable, each verified on a live run rather than only in tests:

- **candidate → model request attribution**, across the worker process boundary
  (§3.7). 12 of 15 candidates attributed; the other 3 unattributable by design.
- **quality per route**, not just health per route — `route_quality.py` plus
  the analysis bridge, the `/route-quality` endpoint, a Control Center panel
  and a dashboard section.
- **a repeatable route A/B**, `scripts/route-experiment.sh`, which pools runs
  per route and refuses to name a winner on thin evidence.
- **operator-labelled mutations** (§4b), which is what makes per-operator
  quality possible at all.

Defects previously listed here are **fixed**:

- the urllib doctor (§3.1)
- the rate limiter's in-process window — it now persists attempt starts to
  `.evolution/nim.window` and restores those still inside the window on start,
  so a restart cannot forget the contract. Corrupt or aged-out state is
  discarded rather than trusted, and the restore is capped so a bad file cannot
  wedge the limiter shut.

Good luck. The measurements in §4 are the most valuable thing here — they point
at where the real wins are, and they were expensive to obtain.
