# Status

What is built, what has actually been measured, and what has not. Kept honest in
both directions: a claim that is stale in the pessimistic direction hides work
that was done, which is the same defect as overclaiming.

Last updated 2026-08-28.

## Engine

`openevolve` 411fb59c (v0.3.2), Apache-2.0, **byte-identical to upstream**.
Enforced by `tests/evolution/test_patch_surface.py`, not merely documented — see
[../patch-surface.md](../patch-surface.md) for the one time it nearly stopped
being true.

## Tests

| Suite | Result |
|---|---|
| Upstream (preserved) | **431 passed** on Windows, 6 failed; **437 passed, 0 failed on Linux** |
| Control plane | **557 passed**, 10 skipped |
| OE-MAX | **411 passed**, 25 skipped |
| BrainPort | **37 passed** |
| Web typecheck | clean |

**1,436 passing.** The six Windows failures are platform, not regression: four
are `openevolve/config.py` opening YAML with no `encoding=`, so Windows decodes
cp1252 and dies on a non-ASCII byte; one asserts a POSIX absolute path survives
unchanged; one expects `ProcessLookupError`, which is POSIX `os.kill` semantics.
They are not fixable without editing the engine. Do not skip them either — a
green suite hiding six failures is worse than an honest six.
Full table in [../testing.md](../testing.md).

CI runs Linux (3.11, 3.12), Windows, and the web/plugin build on every push.

## Providers

| | |
|---|---|
| **Primary** | NVIDIA NIM — leads every role chain (operator decision, 2026-08-27) |
| **Verified** | NIM with a real key, 2026-08-28: 5 of 9 configured ids serve |
| **Verified** | OpenCode Zen free routes — live, keyless, end-to-end evolution; the keyless tail behind NIM in every chain |
| **Removed** | Ox Alpha — withdrawn by the provider 2026-08-26, taken out of service 2026-08-27, including the alternate `stealth/ox-alpha` route |
| **Unverified** | OpenRouter and the 13 catalogue providers — endpoint liveness only; no credential has ever been present, so no inference call has been made |

## Local mode

**Fully supported.** `OE_MAX_LOCAL_ONLY=1` constructs only Ollama, LM Studio,
vLLM and llama.cpp, in **both** routing layers, so no commercial adapter exists
to be dialled.

Verified end to end 2026-08-28 against a local OpenAI-compatible server: 6
requests, 0 failed, every request on the local route. (The score is not quoted:
on this task the unchanged seed spans 0.39 between evaluations, so one run's
number is evidence the machinery ran and nothing more — see ../gotchas.md.)

**A real local LLM now serves.** Verified 2026-08-28 on
`Qwen3.8-27B-Uncensored:iq4_xs` (27.3B, IQ4_XS, 14.26 GB) through Ollama on an
RTX 3050 8 GB / 16 GB RAM box: the broker discovered it from `/v1/models`,
routed to it, and it returned a valid SEARCH/REPLACE diff for the shipped
function-minimization task.

Measured after tuning: **106.8 tok/s prompt, 3.27 tok/s generation**, 39% of the
model resident in VRAM. Method and every intermediate number in
[../local-tuning.md](../local-tuning.md).

**A full evolution run is now driven by a local model.** Verified 2026-08-29,
on `benchmarks/tasks/fn_min_seeded` — the seeded task, without which the scores
below would mean nothing:

| model | iterations | wall | result | score |
|---|---|---|---|---|
| `qwen3:0.6b` | 30 | ~3 min | raised the search budget to 2000 | 1.4513 |
| `qwen3:0.6b` | 300 | ~30 min | **nothing** — zero new bests | 1.4061 (the seed's) |
| Qwen3.5-27B | 19 of 30, stopped | ~180 s/iter | **Differential Evolution**, adaptive F/CR, written at iteration 6 | **1.4987** |

Against the seed's 1.4061, every score re-scored independently at spread 0.

At equal budget the 27B's algorithm is worth +0.090 and the budget change 0.0025,
so its improvement is the algorithm. The 0.6B's whole gain was the budget, and
330 iterations across two runs produced nothing that survived — the 30-iteration
find was a lucky draw. So on this task the small model is a development
instrument and the large one is the tool.

**Through the broker, too.** Verified 2026-08-29: a broker started with
`OE_MAX_LOCAL_ONLY=1` discovered twelve local models, built its chain from that
listing, and served a complete evolution run against
`benchmarks/tasks/fn_min_seeded`.

    ollama/qwen3:0.6b   10 requests, 10 ok, 0 errors, 23,196 tokens, 1055 ms mean

The route is the one `OE_MAX_LOCAL_MODELS` named, so the operator's stated
preference reaches the chain rather than only the adapter.

The broker's own cost is **+2 ms** at the median against calling Ollama directly
(n=10 per arm, alternated). Against a 100–300 s local generation that is not a
number anyone needs to think about again.

## Measurable since the last revision

Five things that were structurally impossible are now measurable, each verified
on a live run rather than only in tests:

- **Whether one model is better than another on this task.** The example task's
  evaluator fixed no seed, so the *unchanged seed program* scored anywhere in
  1.0330–1.4188 — a spread of 0.39, wider than any improvement worth making, and
  a run once reported a new best whose program was byte-identical to the seed.
  `benchmarks/tasks/fn_min_seeded` pins the draws: twenty evaluations, spread
  exactly 0.0. Every comparison in this document rests on it.

- **Candidate → model request attribution**, across the worker process
  boundary. 12 of 15 candidates attributed; the other 3 unattributable by
  design (the seed program and two migrant copies).
- **Quality per route**, not merely health per route — with an endpoint, a
  Control Center panel and a dashboard section.
- **Worker telemetry under `spawn`**, which was silently absent on Windows and
  macOS. One emitting PID before, three after.
- **The container sandbox**, which had never executed outside CI and failed
  four times on its first runs. All four fixed; see [../gotchas.md](../gotchas.md).

## Still open

The honest list. Detail and rationale in [roadmap.md](roadmap.md).

- The BrainPort has never run against a live OpenCode host — every one of its
  37 tests runs against a stub.
- The 44-per-60s rate contract is proven on a virtual clock only; the live NIM
  run peaked at 0 of 44, so it has never been under real pressure.
- Several features are built, gated and unmeasured: multi-offspring, operator
  steering, island policies. They are opt-in for that reason. **Seed Forge is
  no longer among them**: measured 2026-08-29 against the seeded task, two of
  six variants beat the seed and all six score differently — and one of them is
  the exact change, at the exact score, that a 0.6B model took thirty iterations
  to find. See roadmap T8.
- Stock-vs-MAX has not been benchmarked across enough seeds to report.
