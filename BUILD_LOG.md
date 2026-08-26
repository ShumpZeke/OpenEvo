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
   defect in `REQUIREMENTS_PROGRESS.md`.

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

---

## Blocked

**Live NIM and OpenRouter verification.** No `NVIDIA_API_KEY` or
`OPENROUTER_API_KEY` in the environment. The adapters, the global limiter, the
retry/breaker path and the routing logic are implemented and tested offline
against a scripted provider; the HTTP round-trip to those two endpoints is
**unverified**. NIM model discovery is deliberately empty rather than populated
from remembered IDs.

To verify: put the keys in `.env`, then `./scripts/verify-providers.sh`.
