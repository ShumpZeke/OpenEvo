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
| Control plane | **456 passed**, 10 skipped |
| OE-MAX | **359 passed**, 25 skipped |
| BrainPort | **34 passed** |
| Web typecheck | clean |

**1,280 passing.** The six Windows failures are platform, not regression: four
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
requests, 0 failed, `combined_score` 1.4198, every request on the local route.

**Not verified: a real local LLM.** That run used `scripts/local_provider.py` —
a genuine HTTP server, a deterministic generator rather than a model. No Ollama,
LM Studio, vLLM or llama.cpp is installed on the machine this was built on, so
no local model has generated a mutation here. The plumbing is proven; the model
is the open question. See [../local-mode.md](../local-mode.md) and roadmap T0a.

## Measurable since the last revision

Four things that were structurally impossible are now measurable, each verified
on a live run rather than only in tests:

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

- A real local LLM has not generated a mutation here (T0a).
- The BrainPort has never run against a live OpenCode host — every one of its
  34 tests runs against a stub.
- The 44-per-60s rate contract is proven on a virtual clock only; the live NIM
  run peaked at 0 of 44, so it has never been under real pressure.
- Several features are built, gated and unmeasured: multi-offspring, operator
  steering, island policies, Seed Forge. They are opt-in for that reason.
- Stock-vs-MAX has not been benchmarked across enough seeds to report.
