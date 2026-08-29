# Performance

Where time actually goes, measured on the development box (i5-12400F, RTX 3050
8 GB, 16 GB RAM, Windows 11). `local-tuning.md` covers fitting a model to the
GPU; this covers everything else.

Every number here was taken by running the thing, and says what it was compared
against. Where something was measured and *not* changed, the reason is given —
a measurement that did not lead to a change is still worth writing down, because
the next person will otherwise measure it again.

## The shape of a local run

One iteration on the 27B is roughly:

| | |
|---|---|
| generation, ~500 tokens at 3.4 tok/s | **~150 s** |
| prompt processing, ~1000 tokens at 139 tok/s | ~7 s |
| evaluation (the seeded benchmark task) | 0.036 s |
| the evolution loop's own bookkeeping | 0.0002 s |

Generation is 95% of it. That is worth internalising before optimising anything
else here: **the loop is not the bottleneck and neither is the evaluator.** A
change that halves the control plane's work saves a tenth of a second against
two and a half minutes.

The two levers that matter are how many tokens get generated, and how fast the
model generates them. Both are in `local-tuning.md`.

## Disable thinking, or get nothing

The single largest measured effect in this repository. Same prompt, built by the
engine's own sampler, on the 27B:

| `reasoning_effort` | wall | completion | `content` | diff |
|---|---|---|---|---|
| absent | 308.7 s | 978 tok | **0 chars** (3247 chars of reasoning) | none |
| `"none"` | **99.3 s** | 320 tok | 1029 chars | applicable |

Three times the wall clock for nothing usable — and `finish_reason` is `stop`
both times, so it does not present as truncation and raising `max_tokens` does
not help.

Set in two places, deliberately: the broker's local adapter, and
`llm.reasoning_effort` in `configs/oe_max/local.yaml`. Keep both. `api_base` is
a config field, so pointing it straight at Ollama takes the adapter out of the
path along with its setting, and the symptom is every iteration failing with
"No valid diffs found in response" while the model works perfectly.

Setting it in the config applies it to whatever model runs, including a small
one — and a small model's thinking is short enough to fit the budget, so it was
not obvious the same answer held. Measured on `qwen3:0.6b`, n=16 each:

| `reasoning_effort` | applicable diffs | per call | output | per usable diff |
|---|---|---|---|---|
| absent (thinks) | 8/16 | 3.87 s | 762 tok | 7.7 s |
| `"none"` | 7/16 | **0.61 s** | 100 tok | **1.4 s** |

8 against 7 of 16 is noise; 6.3× the speed is not. Right for both sizes, for
different reasons — the large model cannot answer at all without it, the small
one answers six times faster.

## Prompt processing is cached; the prompt defeats the cache

Re-sending a prefix the server has already processed costs 3.5 s against 15.0 s
cold on the 27B — **4×**, reproduced on two independent prefixes.

A KV cache is a *prefix* cache, though, and upstream's `diff_user` template
opens with the fitness score. Consecutive prompts therefore diverge at character
44, and the ~1100-character format specification, which never changes, is
re-processed every iteration. Only 591 characters of a 3938-character prompt are
a reusable prefix.

Reordering the invariant block to the front takes that to 1609 characters,
15% → 41%, worth about **1.4 s per call**.

Not shipped. It is ~1% of a 145 s call, and moving the output-format spec from
the end of the prompt to the beginning may change how often the model emits an
applicable diff — one wasted call is 145 s, a hundred times the saving. The
reordered template and the full numbers are in `configs/local/prompts/`; it is
not wired into any config until the quality question is answered.

## `import openevolve` costs 2.9 seconds

Because it reaches `controller` → `evaluator` → `llm.ensemble` → `llm.openai` →
`openai`, and the OpenAI SDK's pydantic model definitions are 2.24 s of it. Any
process that touches the engine pays for an API client whether or not it will
ever call a model.

| | |
|---|---|
| bare interpreter | 0.10 s |
| `import numpy` | 0.19 s |
| `import openevolve` | **2.97 s** |
| + load an evaluator module | 3.65 s |
| + evaluate the example task once | 4.56 s |

Two places this was worth fixing:

**The seed forge** spawned one child per variant, so three variants cost 14.5 s
of which 13.7 s was Python starting up, to do 0.3 s of evaluation. One child now
scores the whole batch. The isolation that bought is kept where it earns its
price: a variant that raises is reported `null` and dropped, and a child that
dies outright is retried one variant at a time. The sandbox path is deliberately
*not* batched — its purpose is a per-variant limit, and batching would let one
runaway variant spend the whole set's budget.

**BrainPort** declares itself a hard boundary against the engine, and then
imported it: `oe_max/brain/__init__` eagerly imported the one module that
subclasses upstream's `LLMInterface`. Deferred behind a module `__getattr__`,
`import oe_max.brain` went from **4.56 s to 0.15 s**, and a test now fails if
anything makes it eager again.

The engine's own workers still pay it, and that is inherent: they need the
evaluator, and the evaluator's import chain is upstream's.

## Two Windows behaviours that look like something else

Both are in `gotchas.md` with the full detail; they are here because they are
performance-shaped.

**A connect to a closed local port takes 2.03 s**, because Windows drops the SYN
rather than refusing it. That is the OS — a raw socket costs the same, so it is
not the client library and not TLS. `/api/broker` inherited it and took 2352 ms
to report "not reachable" on every poll of the Models view. Splitting the
timeout by phase — 250 ms to connect on loopback, 5 s to read — brought it to
423 ms.

**A run log silently deletes lines it cannot encode.** Upstream marks a new best
with a star; on cp1252 `logging` raises and discards the whole record. Measured:
0 bytes written without `PYTHONIOENCODING`, 48 with. So a run that found a new
best could produce a log that never says so. Set in the runner's child
environment and both run scripts.

## The test suite

The regression gate, so its wall clock is felt on every change.

| | before | after |
|---|---|---|
| upstream (the gate; untouchable) | ~85 s | ~85 s |
| control plane | 89.5 s | **60.0 s** |
| OE-MAX | ~17 s | ~17 s |
| BrainPort | not measured cleanly | not measured cleanly |

The BrainPort row is honest rather than flattering: every timing taken for it
was taken while a 27B evolution run had the machine, and the readings ranged
from 12.7 s to 163 s for the same 37 tests. The deferred import is measured at
the import (4.56 s → 0.15 s) and that number is sound; a suite figure taken
under that much contention is not, and the suite imports `BrainLLM` in its own
tests anyway, so most of the cost is still paid there by design.

Almost all of the control-plane saving was one file. `test_seed_hook.py` scored
variants in child processes using the example evaluator, whose only engine
import is `EvaluationResult` — which drags in the whole chain above. A fixture
evaluator returning a plain metrics dict needs no such import; the child costs
0.3 s instead of 4.6 s, and the file went from **55.9 s for 5 tests to 5.2 s for
16**. One test still uses the real evaluator, so the integration stays covered.

The suites are deliberately still run sequentially. Running them concurrently
would cut wall clock to whatever the upstream suite takes, and would also make
the regression gate depend on four pytest processes not colliding over ports,
temp directories and the telemetry bus singleton. That is a bad trade for a gate.

## Concurrent generation does not help here

The last plausible lever on the dominant cost, and it does not pay.

Generation is memory-bandwidth bound on a partly CPU-offloaded model, and two
requests batched together read the weights once for both — so in principle two
concurrent generations could cost barely more than one. `OLLAMA_NUM_PARALLEL`
was at 1, which serialises them, so this had never been tried.

It does not work, because one generation already saturates all twelve logical
cores. There is no idle capacity for a second to fill, and the attention work
does not share.

| | sequential | concurrent | ratio |
|---|---|---|---|
| concurrency 2, run 3 | 3.16 tok/s | 2.98 tok/s | **0.94×** |
| concurrency 3 | 2.69 tok/s | 1.75 tok/s | **0.65×** |

`OLLAMA_NUM_PARALLEL` is restored to 1, which is the right value.

### The two runs that said 4.6× and 9.6×

Worth writing down, because they were the first two readings and they were
wrong.

| | sequential | concurrent | ratio |
|---|---|---|---|
| concurrency 2, run 1 | 0.59 tok/s | 2.73 tok/s | 4.63× |
| concurrency 2, run 2 | **0.25 tok/s** | 2.42 tok/s | 9.63× |

Look at the sequential column rather than the ratio. A normal reading on this
model is 3.16 tok/s; those arms ran at a fifth and a twelfth of it. The
*concurrent* arms were all normal, around 2.4–3.0. So nothing was fast — the
baseline was slow, because the first arms ran while a 16 GB model was still
settling on a box with under a gigabyte free.

A single run would have reported a mild win (an earlier one-off said 1.16×), and
two of these would have reported a spectacular one. The rule that caught it is
worth keeping: **when a ratio looks too good, read the denominator.**

## Measured and deliberately not changed

| | |
|---|---|
| the evolution loop's bookkeeping | 0.17 ms/iteration under a stub brain — not worth looking at again |
| the seeded evaluator | 36 ms, of which 6 ms is the determinism machinery |
| `control_plane.api` import | 0.15 s |
| every dashboard GET except `/api/broker` | under 130 ms at the median |
| the web bundle | 273 KB JS (80 KB gzipped), 53 modules, 2 runtime dependencies |
| reusing one `httpx.AsyncClient` for the broker probe | would save 165 ms more; binds a client to an event loop for the app's lifetime, which is not worth it in a handler whose job is to report absence |
| `parallel_evaluations` above 1 locally | the model is serial and evaluation is 36 ms; overlapping them saves 0.02% |
| `OLLAMA_NUM_PARALLEL` above 1 | 0.94× at concurrency 2, 0.65× at 3 — one generation already saturates twelve cores |
