# Build Log — OpenEvolve MAX

Chronological record of what was built, what was measured, and what the
measurements changed. Live findings appear here in the order they were
discovered, including the ones that invalidated an earlier assumption.

---

## Task 1 — Machine inspection

Python 3.11.15 · Docker 29.3.1 · 4 cores · 30 GB free · git 2.43.0.
No provider credentials present in the environment:
`OPENCODE_ZEN_API_KEY`, `OPENROUTER_API_KEY`, `NVIDIA_API_KEY` all unset.

## Task 2 — Upstream identity and pin

The build pack names `algorithmicsuperintelligence/openevolve`; the previous
build in this repository used `codelion/openevolve`. Rather than assume one
superseded the other, both were resolved:

```
algorithmicsuperintelligence/openevolve  → 411fb59c886c1870…  refs/heads/main
codelion/openevolve                      → 411fb59c886c1870…  refs/heads/main
```

**Identical commit** — one is a rename/redirect of the other, not a different
project. The existing pin was already correct. Recorded in
`upstream/OPENEVOLVE_PIN.txt`.

## Tasks 3–4 — Environment and stock baseline

Environment already present from the prior build. Upstream's own suite:
**437 passed**, 17 slow deselected. That is the regression gate — it runs
first in `./test.sh`.

## Task 5 — Live OpenCode Zen verification

`GET https://opencode.ai/zen/v1/models` → **HTTP 200 without a key**, 64 models.
`x-preview-f-free` present, as the pack states.

Free-tier models discovered live: `x-preview-f-free`, `deepseek-v4-flash-free`,
`nemotron-3-ultra-free`, `nemotron-3.5-lightning-free`, `mimo-v2.5-free`,
`hy3-free`, `muse-spark-1.2-contributor-free`, `laguna-s-2.1-free`.

## Task 6 — Ox Alpha smoke test, and a client-fingerprint trap

First probe via Python `urllib` returned **HTTP 403 `error code: 1010`** for
every model — a Cloudflare fingerprint block, not a provider outage. `curl` and
`httpx` both returned 200 against the same endpoint at the same moment.

Two consequences:

1. `httpx` is the right client for the broker (it is also what the spec
   suggests), confirmed rather than assumed.
2. **The pre-existing `control_plane/providers/doctor.py` uses `urllib`**, so it
   would report Ox Alpha as unavailable when it is healthy. Recorded as a known
   defect in `requirements.md`.

Live behaviour of `x-preview-f-free` over repeated calls:

| call | result |
|---|---|
| 1 | 200, 2,049 ms |
| 2 | 200, 25,041 ms |
| 3 | **503** |
| 4 | 200, 24,902 ms |

Latency is highly variable and 503s occur. The broker's defaults follow from
this: 180 s provider timeout (a 30 s default would fail healthy calls) and
retry with backoff.

## Task 6b — Issue #44300 re-tested live

The earlier build recorded that Ox Alpha failed on any request carrying a
`tools` array (`anomalyco/opencode#44300`). Re-tested through `httpx`:

```
x-preview-f-free  + tools  → HTTP 200, 7.8s   (no error)
```

**The bug appears resolved.** All five configured Zen models now probe
`tools=True`. The routing exclusion the earlier build applied is therefore
lifted — and, because capability is probed rather than hardcoded, lifting it
required no code change.

## Task 8 — Model availability ≠ model listing

`deepseek-v4-flash-free` is in the `/models` listing and returns
**HTTP 400 "Model is unavailable"** when called. `mimo-v2.5-free` returns
**429 `FreeUsageLimitError`**.

This is the concrete justification for the two-stage discovery in
`providers/registry.py`: list what exists, then prove what serves. A registry
built from the listing alone would route to a dead model.

## Tasks 9–10 — Provider abstraction and broker

One generic OpenAI-compatible adapter serves all three providers; differences
are configuration. Broker exposes `GET /health`, `GET /v1/models`,
`POST /v1/chat/completions`, plus `/v1/oe-max/{status,verify,reset-circuit}`.

Verified live end-to-end: a completion through the broker alias returned
`BROKER_OK`, served by `opencode_zen/x-preview-f-free`, with provenance stamped
onto the response.

## Task 11 — NIM global limiter

Rolling-window guard plus token bucket, on an injectable clock. 17 property
tests assert the invariant over **every contiguous 60-second window**, under
single and 20-way concurrency, with retries counted, across window boundaries,
and with cancellation.

One real bug found while testing: refilling the bucket by exactly the required
amount lands on `0.9999999999`, so a strict `>= 1.0` computed a ~1e-12 wait and
the acquire loop spun forever. Fixed with an epsilon comparison and a minimum
wait; the tests would otherwise have hung rather than failed, which is how it
was noticed.

## Task 13 — OpenEvolve through the broker

Config `configs/oe_max/evolution.yaml` points upstream at
`http://127.0.0.1:8787/v1` with model alias `oe-max-primary`. Zero changes to
OpenEvolve.

**First real run** (8 iterations, Ox Alpha primary):

```
9 requests · 9 ok · 75,889 tokens · avg latency 130,586 ms
best combined_score 1.0586
```

…but **5 of 8 iterations logged `No valid diffs found in response`**. Five
~130-second requests produced nothing usable.

### Root cause

Ox Alpha is a reasoning model. Live usage from the broker:

```
reasoning tokens per request: min 7,986  max 7,997  (of an 8,000 budget)
```

Hidden reasoning consumed essentially the entire completion budget, truncating
the visible diff mid-output. `finish_reason` was `length`.

### Fix, in two places

1. **Broker** — a 200 carrying `finish_reason=length` is now classified
   `TRUNCATED` rather than success, and the router retries with a **doubled
   token budget** (retrying with the same budget reproduces the truncation
   exactly). Escalation stops at a ceiling so a model that cannot fit its
   answer moves to the next route instead of burning the rate budget.
   `reasoning_tokens` is now recorded on every request.
2. **Config** — `max_tokens` raised from 8,000. Tried 32,000 first; latency then
   exceeded OpenEvolve's own client timeout, so **16,000** is the shipped
   default: roughly 8,000 for reasoning plus 8,000 for output.

**Second run, with the fix: 0 diff-parse failures** (previously 5 of 8).

## Tasks 17, 21 — Evaluation gates

G0 (parse/syntax/interface/imports) and G1 (four-strength deduplication:
exact → normalized → AST → structural alpha-renaming). 25 tests, including that
unparseable code is *not* silently treated as a duplicate and that structural
dedup does not collapse programs that merely share a shape.

## Tasks 20, 24 — Operator taxonomy and adaptive selection

All 15 operator classes from the spec, as data with prompt fragments and
applicability rules. Discounted Thompson sampling over Beta posteriors behind a
replaceable `Selector` interface; `UniformRandom` and `EpsilonGreedy` exist so
the "no operator bandit" ablation is a substitution rather than a code removal.

23 tests, including the property that actually motivates discounting: when the
best arm *changes mid-run*, the selector must track it. A stationary bandit
passes "finds the best arm" and still fails the job.

## Task 29 — Terminal dashboard

`python -m oe_max.dashboard`. Shows providers, the **NIM rolling-window count
with headroom** (the number that matters under a rate contract), eligible and
excluded routes with reasons, per-route request/token/latency/error statistics,
the reasoning-token budget warning, and live evolution state.

## Task 30 — Operator scripts

`start-broker` · `run-evolution` · `resume-evolution` · `dashboard` ·
`verify-providers`, in both `.sh` and `.ps1`.

## Task 31 — Attribution across the worker process boundary

The prerequisite for everything below it. Upstream attaches no candidate id to
the generating call — 0 of 22 stored requests carried one — so no post-hoc join
could recover which route produced which candidate.

A ContextVar captured it correctly and was still not enough: the model request
happens in a worker process and `database.add` in the main one, and a live run
produced **3 candidates and 0 attributed**. `Program.metadata` is the only
channel that crosses, so `_run_iteration_worker` is wrapped and stamps it there.

Migrants and stale contexts are left unattributed rather than guessed at —
`_migrate_programs` copies metadata wholesale, and one generation copied to two
islands would otherwise be charged to its route three times.

## Task 32 — Route quality, and the alias problem

`route_quality` measures mutation quality per route in three scarcity views;
`analysis/route_quality` builds it from stored telemetry. Two decisions carried
the value:

**The attempt set is model requests, not candidates.** A route that burns 292
seconds and returns an unusable diff produces no candidate at all; counting
candidates would erase its worst outcome.

**The recorded route is the one that served.** Through the broker the engine
only ever names `oe-max-primary`, so every route collapsed into one row. The
broker's `oe_max` response stamp is now read back and the alias survives as
`requested_model`.

## Task 33 — Multi-offspring, and two silent bugs

Prompting alone cannot do it: upstream applies *every* diff block it finds, so
three alternatives in one response produce one incoherent merge. The parser is
wrapped so the primary child is byte-for-byte what it would be at N=1.

Both bugs failed silently. The preamble was being treated as alternative 1 — and
applying a diff-free string returns the parent unchanged, so the run looks
healthy and evolves nothing. And `apply_diff` calls `extract_diffs` internally,
so the wrapper re-entered and wiped the stash: the first live run produced zero
siblings and no error.

Raw yield 1.00 → 2.42 per request; **distinct** yield 0.58 → 0.75. The gap is
the whole risk, and reporting the raw number alone would have been a 2.4x
overclaim.

## Task 34 — Operators, island policies, Seed Forge

The taxonomy and the bandit had existed since the OE-MAX build with no way to
reach a live run. `PromptSampler.build_prompt` is wrapped to ask for a named
mutation class, and the label rides the attribution channel through to
`candidates.gen_operator`.

Island policies gave `Operator.disruption` — declared on all 15 operators and
read by nothing — a consumer. Sharpness was measured rather than picked: at 2.0,
exploit lands at 0.314 and explore at 0.712 against an unweighted 0.571.

Seed Forge builds a starting population from one seed with no model requests: 7
valid distinct variants, two of them beating the seed. With the caveat attached
— they won by running the search *harder*, which is local compute traded for
score.

## Task 35 — Verification, and sandboxed execution

V1 property/metamorphic/randomized checks, proven by feeding it four programs
that cheat rather than four that work. Hard-coding the answer passes every
single-run property and only a metamorphic relation catches it; a score that is
a lucky draw is caught by determinism alone.

Candidates now run in a separate process under real ceilings
(`OE_MAX_SANDBOX_EVAL`), and `describe_backends()` states what each backend does
**not** stop. The container backend reported itself available on the strength of
the docker binary; it now probes the daemon, which is unreachable here.

## Task 36 — Running everything at once

Six opt-in features had each been tested alone. Doing it together found three
bugs no unit test could: `migrant` was never written to the projection, so two
analysis modules were measuring the wrong population while their tests passed;
`island_policy` never reached an event, making the policy layer unmeasurable;
and a null token count rendered as «redacted».

The general rule, now in the handoff: a flag that lives only on the in-memory
`Program` is a filter that silently never matches.

## Task 37 — Route demotion

Ox Alpha degraded 40% → 26% → 11% success across one session while the circuit
breaker sat closed. The reason is exact: the breaker trips on N failures inside
a rolling 60-second window, and Ox Alpha's requests take ~300 seconds, so each
failure ages out before the next arrives.

A breaker whose window is shorter than the request latency is inert — and inert
for the slowest routes, where a wasted request costs most. `RouteHealth.degraded`
counts attempts instead of seconds. If every route is degraded the least bad
still serves, because a run that dies with "no usable route" is worse than one
served slowly.

---

## Session: the primary route had stopped existing (2026-08-26)

Re-probed every configured provider and found the routing layer pointed at
three models that could not serve, one of which was the primary.

**What was wrong**

| | |
|---|---|
| `x-preview-f-free` (Ox Alpha) | withdrawn from Zen — absent from `/models`, `ModelError: not supported`. Headed every chain in both routing layers. |
| `deepseek-ai/deepseek-v4-pro` | not in NVIDIA's catalogue; configured as the "strong fallback". |
| `qwen/qwen2.5-coder-32b-instruct` | not in NVIDIA's catalogue. NIM hosts no Qwen model at all. |
| Zen `deepseek-v4-flash` | configured `requires_key=False`; returns 401 without a key. |

The withdrawal arrives as HTTP **401** with a ModelError body, so it reads as a
credential problem until you look at the body — which is how it went unnoticed.

**What changed**

- Routing is per role (reasoner / coder / judge / fast), addressed by broker
  alias so the engine is untouched. The free Zen routes differ in *kind*: only
  `laguna-s-2.1-free` reports zero hidden-reasoning tokens, and it now judges.
  End to end the judge route answers in 859 ms against the reasoner's 8,158 ms.
- `Registry.reconcile()` crosses the live listing against what is configured on
  every discovery. Two-stage discovery asked "is a listed model serveable?" and
  never the converse. Run against the real endpoints it found all three dead
  models on its own, keylessly — both Zen's and NIM's listings are public.
- Providers are configuration (`configs/oe_max/providers.yaml`, 15 of them,
  key-gated). The catalogue names *patterns*, never model ids; concrete ids are
  materialised from each provider's own listing.
- An exhausted free allowance is no longer treated as a rate limit. Both are
  429 and Zen's free-limit message even says "try again later"; only the type
  distinguishes them. It now parks the route instead of spending four retries.
- Retrying one route is bounded by wall-clock, not only attempt count. The
  primary averaged 90 s per request, so four attempts is six minutes with a
  working fallback idle.
- A single failed probe no longer demotes a route. Caught live: laguna failed
  one probe between two successful runs, which would have removed the judge and
  fast roles' leading route until someone re-ran verification.
- The Control Center showed the control plane's router while the *broker* did
  the routing. `GET /api/broker` and a new panel show what actually serves.
- GPU monitoring implemented — `RESOURCE_GPU` had been declared and never
  emitted.

**Verified end to end**: bootstrap, broker, an 8-iteration evolution run
(7 programs, four islands, best 1.4607 from 0.7614), checkpoint resume
(7 programs restored with generation counters intact), dashboard, provider
verification, Control Center. Tests 437 + 327 + 295.

---

## Blocked

**Live NIM and catalogue-provider verification.** No credential for any of them
exists here. Endpoint liveness is verified for all 15, and NIM's model ids are
**catalogue-verified** against its public listing — but not one inference call
has been made, and `available` stays `None` rather than `True`.

To verify: put a key in `.env`, then `./scripts/verify-providers.sh`.

**The container execution backend.** `docker` is on PATH here and its daemon is
not reachable, so the `--network none` / read-only-root / dropped-capabilities
path has never run. The probe reports it unavailable with the daemon's own
error rather than assuming.

**Ox Alpha.** No longer blocked, because it no longer exists. It was never
benchmarkable at 11% success and ~300 s per request, and on 2026-08-26 it was
withdrawn from Zen entirely. The route that replaced it, `nemotron-3-ultra-free`,
measured 67% success and ~90 s per request over a real run — better, and still
the flakiest part of the system.

**The PowerShell scripts.** `verify-providers.ps1` and `ablation.ps1` were added
for parity and have **never been run**: there is no PowerShell in this
container. The Python they call is tested.
