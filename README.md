# Evolution

[![tests](https://github.com/ShumpZeke/OpenEvo/actions/workflows/ci.yml/badge.svg)](https://github.com/ShumpZeke/OpenEvo/actions/workflows/ci.yml)
[![licence: Apache-2.0](https://img.shields.io/badge/licence-Apache--2.0-blue.svg)](LICENSE)
[![upstream: OpenEvolve 411fb59c](https://img.shields.io/badge/upstream-OpenEvolve%20411fb59c-lightgrey.svg)](https://github.com/codelion/openevolve)

> **A second execution path landed on 2026-08-27: the OpenCode BrainPort.** It
> is additive and **not yet the default** — be precise about which path you are
> reading about, because the two do not share a loop.
>
> - **The shipping path** is unchanged: upstream's engine, driven through the
>   OE-MAX provider broker on `:8787`. `./scripts/run-evolution.sh` uses this,
>   and every live measurement in [BENCHMARKS.md](BENCHMARKS.md) came from it.
> - **The BrainPort path** (`oe_max/brain/`) makes the *model* someone else's
>   problem: OpenCode owns provider, credentials, catalog and model switching,
>   and `brain.mode = inherit` means whatever is selected there is what runs.
>   It is driven by the OpenCode plugin in `packages/opencode-plugin/` and runs
>   its own lighter evolution loop in `oe_max/brain/evolution.py` — it does not
>   go through upstream's controller, MAP-Elites or island machinery.
>
> **Verification status, stated plainly:** 34 tests cover the BrainPort, and 26
> acceptance gates pass (`scripts/verify-brainport-acceptance.ps1`). All of them
> run against `NullBrainPort` or the stdio worker. **No BrainPort run against a
> live OpenCode host has been recorded**, so the claim "zero source changes when
> a model disappears" is proven structurally — no model ID or provider URL
> exists in the core, and a test enforces that — and not yet empirically.
>
> The legacy provider stack (`oe_max/providers`, `oe_max/router`, `oe_max/limiter`,
> `control_plane/providers`) is marked deprecated and quarantined behind
> `oe_max/brain/legacy_adapter.py`. It is **still what the default path runs on**
> and must not be deleted until that path moves. `scripts/legacy_deletion_gate.py`
> is the check that says when it may. See [oe_max/brain/README.md](oe_max/brain/README.md).

A production fork of [OpenEvolve](https://github.com/codelion/openevolve) that
keeps the evolutionary engine exactly as upstream ships it and adds a real-time
control plane around it: a browser Control Center, typed telemetry, a query and
control API, health- and capability-aware model routing, and an isolated agent
sandbox.

Forked from upstream `411fb59c` (v0.3.2, Apache-2.0). The engine is
**byte-identical to upstream** — see [PATCH_SURFACE.md](PATCH_SURFACE.md).

> **Continuing this work?** Start with **[HANDOFF.md](HANDOFF.md)** — current
> state, the traps that cost real time to find, and live provider measurements.
> Then **[NEXT_TASKS.md](NEXT_TASKS.md)** for a prioritised queue.

---

## Two layers

**OE-MAX** — a local OpenAI-compatible **provider broker** on `127.0.0.1:8787`.
OpenEvolve points at it and knows nothing about providers: routing, the NIM rate
contract, failover, retry and credential ownership all live behind that one
base URL. Routing is **per role** — reasoner, coder, judge, fast — each with its
own chain, addressed by model alias so the engine needs no changes.

**Control plane** — telemetry, storage, query/control APIs and a 19-view browser
Control Center over live evolution.

Both sit *around* upstream. The engine is byte-identical.

## What it adds

- **Control Center** — 19 views over live evolution: lineage graph, MAP-Elites
  Lab with a generation scrubber, island and migration analysis, candidate
  inspector with parent diffs, model and evaluator observability, checkpoints,
  traces, run comparison, system health.
- **Real telemetry** — a typed event model instrumented at the engine boundary.
  Every value the UI shows traces to an emitted event; there are no fixtures or
  placeholder metrics anywhere in the frontend.
- **Provider routing** — **NVIDIA NIM is the primary provider**, leading every
  role chain, with the keyless OpenCode Zen routes behind it and thirteen other
  OpenAI-compatible providers available by configuration. Routing is driven by
  live health *and* verified capabilities. A route whose credential is absent is
  filtered out rather than attempted, so a checkout with no `NVIDIA_API_KEY`
  falls straight through to the keyless routes and still runs. Providers are
  configuration (`configs/oe_max/providers.yaml`) and their model ids are
  discovered from each provider's own listing rather than written down.
- **Route quality, not just route health** — every candidate is attributed to
  the model request that generated it, so the question "which route produces
  better mutations, and at what cost?" is answerable from a run's own
  telemetry. `./scripts/route-experiment.sh` runs one arm per route and refuses
  to name a winner on thin evidence.
- **Agent sandbox** — OpenCode as an optional evaluation backend, in an
  environment that cannot reach the operator's own OpenCode installation.
- **Everything upstream still works** — the original CLI, configs, examples,
  checkpoint/resume, and the classic visualizer are preserved and reachable.

## Quick start

```bash
git clone https://github.com/ShumpZeke/OpenEvo.git
cd OpenEvo
./bootstrap.sh                    # environment, deps, storage, UI build, checks
```

`bootstrap` creates `.venv`, installs the engine and control plane with the
`[dev]` extra, initialises storage, builds the Control Center and the OpenCode
plugin, and runs a smoke test. It installs nothing globally and modifies nothing
outside this directory. On Windows use `.\bootstrap.ps1`.

Requirements: Python 3.10+ and git. Node 20+ is optional — without it the API
still runs, it just serves no browser UI.

Then, in two terminals:

```bash
./scripts/start-broker.sh         # OE-MAX broker → 127.0.0.1:8787
./scripts/run-evolution.sh --task function_minimization --iterations 20
```

Watch it live:

```bash
./scripts/dashboard.sh            # providers, rate windows, routes, evolution
./run.sh                          # browser Control Center → 127.0.0.1:8000
```

### Coming back to it later

```bash
./scripts/memory.sh                 # where you left off
./scripts/memory.sh note "..."      # leave yourself something
./scripts/memory.sh search "..."    # find it again
```

Prints your run history, the high-water score, every run you can resume with
the exact command to do it, and your journal. Runs started from the shell are
imported automatically, so history means the same thing however you launched.
The same thing lives in the Control Center's **Memory** view.

**No API key is required to try it.** OpenCode Zen serves four free models
without one — verified 2026-08-26 — and `./scripts/verify-providers.sh` shows
what is actually reachable right now. Every other provider is key-gated and
inert until you add its key, so the shipped configuration works as-is.

Windows: `.\bootstrap.ps1` then `.\run.ps1`.

Try it with no API key at all — a local OpenAI-compatible endpoint is included:

```bash
./run.sh provider &                        # 127.0.0.1:8765
./run.sh                                   # then start a run from Experiments,
                                           # or via the API:
curl -X POST http://127.0.0.1:8000/api/control/runs \
  -H 'Content-Type: application/json' -d '{
    "initial_program": "examples/function_minimization/initial_program.py",
    "evaluator":       "examples/function_minimization/evaluator.py",
    "config_path":     "configs/evolution/local_test.yaml",
    "iterations": 24, "name": "first-run" }'
```

### Using NVIDIA NIM

NIM is the primary provider and leads every role chain. One key turns it on:

```bash
NVIDIA_API_KEY=nvapi-...      # primary — leads every role chain
OPENCODE_API_KEY=...          # optional; Zen's free routes need no key at all
```

Five of the nine configured NIM models serve, measured with a real key on
2026-08-28: `nemotron-3-super-120b-a12b` (732 ms with tools — the fastest
working route measured on any provider here), `nemotron-3-ultra-550b-a55b`
(4.5 s, the flagship reasoner), `nemotron-3-nano-30b-a3b`, `kimi-k3` (11.5 s,
the code specialist) and `deepseek-v4-flash-0731` (51 s). The other four are
shipped disabled with the reason attached — a hang, a 400, a 404 entitlement
error and a standing 429. See [PROVIDERS.md](PROVIDERS.md) for the table.

Without the key nothing breaks: a route whose credential is absent is filtered
out rather than attempted, so the chain falls through to the keyless Zen routes
and the run still works.

Then run the provider doctor (Models → *run provider doctor*) to probe what is
actually available right now.

## The original tooling, unchanged

```bash
.venv/bin/openevolve-run --help                    # upstream CLI, untouched
./run.sh classic path/to/checkpoint_20             # classic visualizer :8080
./run.sh cli examples/function_minimization/initial_program.py \
              examples/function_minimization/evaluator.py --iterations 50
```

The classic visualizer runs as its own service on its own port. It is preserved,
not reimplemented — it keeps working whether or not the Control Center is up.

## Layout

```
openevolve/         upstream engine — byte-identical, never edited
oe_max/
  limiter.py        global rate limiter (hard rolling-window invariant)
  providers/        adapter, registry, live discovery + smoke tests
  broker/           OpenAI-compatible broker OpenEvolve points at
  router.py         chain selection, failover, retry, truncation escalation
  route_quality.py  per-route mutation quality (three scarcity views)
  health.py         circuit breaker and rolling health
  evaluation/       G0 validity + G1 four-strength deduplication
  search/           mutation taxonomy + discounted Thompson sampling
  archives.py       hall of fame, Pareto front, novelty, failure memory
  verification/     V1 property/metamorphic/randomized checks + counterexamples
  execution/        sandboxed candidate execution with real resource ceilings
  dashboard.py      terminal dashboard
scripts/            operator scripts (.sh and .ps1) + upstream visualizer
control_plane/
  telemetry/        event model, redaction, bus, engine instrumentation
  storage/          SQLite event log + derived projections
  api/              control, query and SSE streaming
  providers/        profiles, runtime doctor, health/capability router
  analysis/         route quality built from stored telemetry
  sandbox/          OpenCode isolation boundary
  runner/           engine subprocess lifecycle
web/                Control Center (React + TypeScript + Tailwind)
tests/              upstream suite (untouched) + tests/evolution
```

## Tests

```bash
./test.sh
```

Measured on Windows 11 / CPython 3.11.9:

| Suite | Result |
|---|---|
| Upstream OpenEvolve (preserved) | **431 passed**, 6 failed (Windows-only, below), 17 slow deselected, 43 subtests |
| Control plane | **402 passed**, 10 skipped |
| OE-MAX (broker, limiter, gates, search, archives, verification, execution) | **306 passed**, 24 skipped |
| BrainPort (OpenCode brain, worker, plugin contract) | **34 passed** |
| Web typecheck | clean |

The six upstream failures are platform, not regression: four are
`openevolve/config.py` opening YAML with no `encoding=`, so Windows decodes it
as cp1252 and dies on a non-ASCII byte; one asserts a POSIX absolute path
survives unchanged; one expects `ProcessLookupError`, which is POSIX `os.kill`
semantics. All six pass on Linux, which is what CI runs. They are not fixable
here — `openevolve/` is byte-identical and stays that way. The full table with
causes is in [TEST_STRATEGY.md](TEST_STRATEGY.md).

The upstream suite runs first: a change that breaks it is a regression in the
fork, not merely a control-plane bug.

## Documentation

| | |
|---|---|
| **[CONTRIBUTING.md](CONTRIBUTING.md)** | **clone, build, test, and the rules that are load-bearing** |
| **[HANDOFF.md](HANDOFF.md)** | **start here if you are continuing this work** |
| **[NEXT_TASKS.md](NEXT_TASKS.md)** | **prioritised work queue with rationale** |
| [ARCHITECTURE.md](ARCHITECTURE.md) | how the pieces fit and why |
| [DECISIONS.md](DECISIONS.md) | engineering decisions and their evidence |
| [TELEMETRY.md](TELEMETRY.md) | event model, transport, no-fake-data rule |
| [PROVIDERS.md](PROVIDERS.md) | what each provider actually does, verified; and the dead-endpoint list |
| [SANDBOX.md](SANDBOX.md) | OpenCode isolation boundary |
| [SECURITY.md](SECURITY.md) | secret handling and redaction |
| [PATCH_SURFACE.md](PATCH_SURFACE.md) | every upstream file touched (none) |
| [UPSTREAM_SYNC_STRATEGY.md](UPSTREAM_SYNC_STRATEGY.md) | merging future releases |
| [FEATURE_COVERAGE_MATRIX.md](FEATURE_COVERAGE_MATRIX.md) | requirement-by-requirement status |
| [TEST_STRATEGY.md](TEST_STRATEGY.md) | what is tested and what is not |
| [REQUIREMENTS_PROGRESS.md](REQUIREMENTS_PROGRESS.md) | OE-MAX spec coverage, gaps and blockers |
| [BUILD_LOG.md](BUILD_LOG.md) | what was built and what the measurements changed |
| [BENCHMARKS.md](BENCHMARKS.md) | live measurements from the real primary route |

## Three things worth knowing up front

**A route can stop existing.** A stealth-preview model was this project's
primary until the provider withdrew it — probed and found absent from `/models`,
answering `ModelError: ... is not supported`. It has since been removed from
service here entirely. Free status is a runtime-probed value, never rendered as
unlimited, and the model tables are reconciled against each provider's live
listing on every discovery so a withdrawal disables itself. That is also how
`openai/gpt-oss-120b` and `mistralai/codestral-22b-instruct-v0.1` came out of the
NIM chains: probed with a real key, the first hung and the second returned a 404
entitlement error. See [PROVIDERS.md](PROVIDERS.md).

**A reasoning model can spend its whole budget thinking.** One former primary was
measured using 7,986–7,997 of an 8,000-token budget on *hidden* reasoning,
truncating the visible diff — 5 of 8 iterations produced nothing from
~130-second requests. Several current routes reason too. The broker detects
`finish_reason=length` and retries with a doubled budget, and `max_tokens`, the
provider timeout and the client timeout are tuned together. It is also why
judging routes to a different model than mutation: ranking candidates does not
need hidden thought, so it goes to the cheapest fast route rather than the
flagship.

**A listed model is not a working model, and an unlisted one is not a model.**
Zen lists `deepseek-v4-flash-free` and returns "Model is unavailable" for it, so
discovery is two-stage: list, then smoke-test. The converse caught a withdrawn
primary and two NVIDIA models this project had configured that were never in
NVIDIA's catalogue at all. The sharpest case: `nvidia/nemotron-nano-3-30b-a3b`
returns 404 while `nvidia/nemotron-3-nano-30b-a3b` serves — two transposed
words, both in the catalogue, one of them fictional in practice.

## Licence

Apache-2.0, inherited from OpenEvolve. Upstream retains copyright over
`openevolve/`, `scripts/visualizer.py`, `configs/` and `examples/`.
