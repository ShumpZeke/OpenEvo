# Benchmarks — OpenEvolve MAX

What was actually measured, on what, and what it changed. Numbers here come
from live runs against the real primary route; nothing is projected or
estimated.

The spec's objective is roughly

```
verified improvement × diversity × verification confidence × breakthrough magnitude
────────────────────────────────────────────────────────────────────────────────
        wall-clock × API requests × local compute
```

so the denominators — requests and wall-clock — matter as much as the score.
The most consequential findings below are all denominator findings.

---

## Environment

| | |
|---|---|
| Task | `examples/function_minimization` (upstream, unmodified) |
| Engine | OpenEvolve `411fb59c` (v0.3.2), unmodified |
| Primary route | OpenCode Zen `x-preview-f-free` (Ox Alpha Free) |
| Path | OpenEvolve → OE-MAX broker (127.0.0.1:8787) → Zen |
| Machine | 4 cores, 15 GiB RAM, Linux |
| Seed | 42 |
| Credentials | none — Zen served without an API key |

---

## Measurement 1 — Ox Alpha latency and reliability

Repeated trivial completions:

| call | outcome | latency |
|---|---|---|
| 1 | 200 | 2,049 ms |
| 2 | 200 | 25,041 ms |
| 3 | **503** | 424 ms |
| 4 | 200 | 24,902 ms |

Under real evolution load, per-request latency averaged **130,586 ms** with
~8,000 tokens per request.

**Consequences.** A conventional 30 s client timeout fails healthy calls, so the
broker's provider timeout is 600 s. 503s occur, so retry with backoff is not
optional. And at ~130 s per generation, anything that wastes a request is
expensive — which is what makes the cheap gates and the finding below matter.

## Measurement 2 — reasoning tokens destroy the output budget

The most important measurement of the build.

```
reasoning tokens per request:  min 7,986   max 7,997   (of an 8,000 budget)
```

Ox Alpha is a reasoning model and spent essentially the **entire** completion
budget on hidden reasoning, leaving nothing for the visible diff.

| run | config | diff-parse failures |
|---|---|---|
| 1 | `max_tokens: 8000`, no truncation handling | **5 of 8 iterations** |
| 2 | `max_tokens: 8000` + broker truncation detection | **0** |

Five ~130-second requests produced nothing at all. In the spec's terms, roughly
**60% of the API-request denominator was being spent for zero numerator.**

**Fixes, both evidence-driven:**

1. The broker classifies a 200 carrying `finish_reason=length` as `TRUNCATED`
   and retries with a **doubled** token budget. Retrying with the same budget
   reproduces the truncation exactly, so escalation is the only retry that can
   work. It stops at a ceiling and moves to the next route rather than growing
   forever.
2. `max_tokens` raised to 16,000 — about 8,000 for reasoning plus 8,000 for
   output.

## Measurement 3 — provider timeout must scale with the token budget

Raising `max_tokens` to 16,000 exposed a second-order effect: requests then ran
past the broker's 180 s provider timeout.

```
12 requests · 0 ok · 4 truncated · 2 unavailable · 6 timeout · avg 141,110 ms
```

Not a provider fault — a configuration mismatch created by fixing the first
problem. Zen's provider timeout is now 600 s.

The general lesson, recorded because it will recur with any reasoning model:
**token budget, provider timeout and client timeout are one coupled setting.**
Changing one alone converts a truncation failure into a timeout failure.

## Measurement 4 — free-model availability is not uniform

Probed live across Zen's free tier:

| model | serves | tools | latency |
|---|---|---|---|
| `x-preview-f-free` (Ox Alpha) | yes | **yes** | 1,969 ms |
| `nemotron-3-ultra-free` | yes | yes | 829 ms |
| `nemotron-3.5-lightning-free` | yes | yes | 1,271 ms |
| `laguna-s-2.1-free` | yes | yes | 1,855 ms |
| `hy3-free` | yes | yes | 2,444 ms |
| `deepseek-v4-flash-free` | **no** — HTTP 400 "Model is unavailable" | — | — |
| `mimo-v2.5-free` | **no** — HTTP 429 `FreeUsageLimitError` | — | — |

Two of seven listed free models do not serve. This is the empirical case for
two-stage discovery: listing then smoke-testing.

**Ox Alpha now supports tools.** The earlier build recorded
`anomalyco/opencode#44300` (tools requests failing with "Endpoint is
unavailable"); re-tested live, tools requests return 200. Because capability is
probed rather than hardcoded, no code change was needed to re-admit it.

## Measurement 5 — rate limiter

Not a live measurement — a proof. 17 property tests assert
`attempt starts ≤ 44 in every contiguous 60-second window` on a virtual clock,
under 1 and 20 concurrent workers, with retries counted, across window
boundaries, under burst-then-idle, and with cancellation. A test also asserts
the limiter is *not* over-conservative (peak window ≥ 35/44), because a limiter
that grants nothing trivially satisfies the bound.

---

## Stock vs MAX

**Harness ready; a multi-seed comparison has not been executed.**

`./scripts/run-evolution.sh --profile stock|max` runs two arms that differ in
exactly one thing — the path to the provider:

```
stock : OpenEvolve  ────────────────────────────────►  Zen  (x-preview-f-free)
max   : OpenEvolve  ──►  OE-MAX broker  ────────────►  Zen  (x-preview-f-free)
```

Same task, evaluator, seed, population, islands, MAP-Elites dimensions,
iteration budget **and model**. Holding the model fixed is what makes the result
attributable to the system rather than to the model — a baseline on a different
model would measure the models. `configs/oe_max/stock_baseline.yaml` is the
baseline arm, and it is runnable with no key because Zen serves this route
without one.

What the MAX arm adds, and therefore what the comparison measures: truncation
detection with budget escalation, retry/backoff, circuit breaking, route
failover, and per-request provenance.

Honest reason: at ~130 s per generation on the primary route, a single 10-
iteration arm takes 20–30 minutes, and the spec requires multiple seeds and ten
ablations. Reporting a one-seed, eight-iteration difference as a benchmark
result would be noise presented as evidence.

The baseline arm is also expected to be *unusually* penalised on this route,
which is worth stating in advance rather than discovering afterwards: without
truncation escalation it inherits the 5-of-8 failure mode measured above. That
is a real difference, but it is a difference in provider handling, not in
search quality, and the comparison writeup should separate the two.

What *can* be said from the runs performed:

| run | config | diff failures | best score |
|---|---|---|---|
| 1 | 8k tokens, no truncation handling | **5 of 8** | 1.0586 |
| 2 | 8k + truncation detection | 0 (cut short) | 1.0586 |
| 3 | 16k + truncation detection + 600s provider timeout | **0 in 9 iterations** | **1.4995** |

The demonstrated improvement is in **request efficiency**, not a claim about
search quality: the same search now wastes far fewer of its expensive requests.
Run 3 also found a genuine improvement, `1.0586 → 1.4995 (+0.4409)`, but with
one seed and ten iterations that is an anecdote, not a benchmark.

### Run 3, in full

**The run did not finish.** It completed 9 of 10 iterations and was then killed
by a 2400-second wall-clock cap I had set on the command — exit 124, my own
limit, not an engine or provider failure. A checkpoint was written at iteration
6 and the run is resumable:

```bash
./scripts/resume-evolution.sh runs/max-final
```

Numbers as measured over those 9 iterations:

```
22 requests to Ox Alpha · 9 ok (41%) · 128,958 tokens · avg 229 s
   errors: 10 transport, 1 unavailable, 1 server, 1 truncated
 1 request  to nemotron-3-ultra-free · 1 ok (100%) · 6,107 tokens · avg 112 s
 0 diff-parse failures · 2 client timeouts
 best score 1.0586 → 1.4995
```

The headline result — 0 diff-parse failures against 5 of 8 before the fix —
holds regardless of the run being cut short, because it is a ratio over the
iterations that did run. The 2 client timeouts are worth noting rather than
rounding away: at ~229 s average against a 900 s client limit, a slow request
plus retries can still exceed it. That is the coupling described in Measurement
3 and it has not been fully eliminated, only reduced.

Two things this establishes beyond the truncation fix:

1. **Ox Alpha's reliability under sustained load is ~41%**, dominated by
   transport errors rather than rate limiting. Over three separate runs it has
   been consistently slow (avg 130–229 s) and consistently flaky. That is not a
   reason to drop it — it is the operator's chosen primary and it produced the
   improvement — but it is the reason the retry, circuit-breaker and failover
   machinery is load-bearing rather than decorative.

2. **The failover chain works in production.** When Ox Alpha degraded, traffic
   moved to `nemotron-3-ultra-free`, which returned 100% success at half the
   latency. That was never explicitly exercised in a test; it happened on its
   own under real conditions.

The second point is what makes the fast-model routing experiment (below) the
highest-value next step: a route that is ~2× faster and, on this sample,
substantially more reliable is already quietly picking up the slack.

## Route health moves, so a table is a snapshot

Ox Alpha's own numbers changed materially within a week, on the same task
through the same broker:

| measured | success | p50 latency | attempts |
|---|---|---|---|
| first run | 40% | 220 s | 15 |
| later | 26% | **284 s** | 23 |

Two single requests during that later window returned in 294.8 s and 502.6 s.
This is the argument for re-running the route experiment rather than trusting
any table here, including this one — and for `route_quality`'s refusal to
recommend a routing change from a thin sample.

For contrast, on the same day, against the *same* evolution prompt:

| route | mean latency | note |
|---|---|---|
| `nemotron-3-ultra-free` | **83.6 s** | 12 attempts, 100% valid, 0 duplicates, 58% improved |
| `hy3-free` | ~261 s | 2 s on a trivial probe — the prompt is what makes it slow |
| `x-preview-f-free` | 284 s p50 | 26% success |

The `hy3-free` row is worth keeping precisely because it is embarrassing for
quick probes: a route that answers a one-line prompt in 2 seconds took ~4
minutes on a real evolution prompt. Capability probes measure reachability,
not throughput.

## Multi-offspring, measured locally

`OE_MAX_MULTI_OFFSPRING=3`, 12 iterations, local provider:

| | N=1 | N=3 |
|---|---|---|
| mutation requests | 12 | 12 |
| candidates | 13 | 30 |
| **candidates per request** | **1.08** | **2.50** |
| extra offspring | 0 | 17 |
| best score, primaries | 1.4045 | 1.4045 |
| best score, siblings | — | **1.4953** |

The siblings out-scored the primary children, so they compete on merit rather
than padding the archive.

**What this does not show.** The local provider draws from a fixed pool of five
mutations, so its duplicate rate says nothing about a real model — and the
duplicate rate is the number that decides this feature. Three near-identical
alternatives collapsing to one AST hash is throughput that is not real. That
measurement needs a real provider and has not been run.

## What to measure next

Ordered by expected value given the measurements above:

1. **Multi-offspring on a real provider.** Built and measured locally (above);
   the duplicate rate on a real model is the number that decides it.
2. **Stock vs MAX, ≥5 seeds**, measuring area under the best-so-far curve
   against *requests* rather than wall-clock.
3. **Operator bandit ablation** — `uniform_random` versus
   `discounted_thompson`, already a one-line substitution.
4. **Fast-model routing.** `laguna-s-2.1-free` and
   `nemotron-3.5-lightning-free` are 50–100× faster than Ox Alpha. Whether Ox
   Alpha's quality justifies its latency for *every* operator class is an open
   empirical question the route statistics are already collecting data for.
