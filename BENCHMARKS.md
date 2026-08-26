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

**Not yet run as a controlled comparison.** The harness exists —
`./scripts/run-evolution.sh --profile stock|max` selects upstream's own config
or the broker config against an identical task, evaluator and seed — but a
multi-seed comparison has not been executed.

Honest reason: at ~130 s per generation on the primary route, a single 10-
iteration arm takes 20–30 minutes, and the spec requires multiple seeds and ten
ablations. Reporting a one-seed, eight-iteration difference as a benchmark
result would be noise presented as evidence.

What *can* be said from the runs performed:

| run | config | requests | ok | tokens | diff failures | best score |
|---|---|---|---|---|---|---|
| 1 | 8k tokens, no truncation handling | 9 | 9 | 75,889 | 5 of 8 | 1.0586 |
| 2 | 8k + truncation detection | — | — | — | **0** | 1.0586 |

The improvement demonstrated is in **request efficiency**, not in final score:
the same search now wastes far fewer of its expensive requests.

## What to measure next

Ordered by expected value given the measurements above:

1. **Multi-offspring (spec §7F).** At ~130 s and ~8,000 tokens per request,
   getting 2–3 diverse candidates from one request is close to a linear
   throughput win. This is the single highest-value experiment on this route,
   and the latency measurement is what makes that clear.
2. **Stock vs MAX, ≥5 seeds**, measuring area under the best-so-far curve
   against *requests* rather than wall-clock.
3. **Operator bandit ablation** — `uniform_random` versus
   `discounted_thompson`, already a one-line substitution.
4. **Fast-model routing.** `laguna-s-2.1-free` and
   `nemotron-3.5-lightning-free` are 50–100× faster than Ox Alpha. Whether Ox
   Alpha's quality justifies its latency for *every* operator class is an open
   empirical question the route statistics are already collecting data for.
