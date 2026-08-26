# Evolution

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
base URL. Ox Alpha (OpenCode Zen) is the primary route.

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
- **Provider routing** — OpenCode Zen / Ox Alpha Free preferred by default, with
  automatic fallback to NVIDIA NIM and other OpenAI-compatible endpoints,
  driven by live health *and* verified capabilities.
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
./bootstrap.sh                    # environment, deps, storage, UI build, checks
```

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

**No API key is required to try it.** OpenCode Zen was observed serving
`x-preview-f-free` (Ox Alpha) without one — `./scripts/verify-providers.sh`
shows what is actually reachable right now.

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

To use a real provider, put a key in `.env`:

```bash
OPENCODE_API_KEY=...     # preferred primary route
NVIDIA_API_KEY=...       # strong fallback
```

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

| Suite | Result |
|---|---|
| Upstream OpenEvolve (preserved) | **437 passed**, 17 slow deselected |
| Control plane | **127 passed** |
| OE-MAX (broker, limiter, gates, search, archives) | **135 passed** |
| Web typecheck | clean |

The upstream suite runs first: a change that breaks it is a regression in the
fork, not merely a control-plane bug.

## Documentation

| | |
|---|---|
| **[HANDOFF.md](HANDOFF.md)** | **start here if you are continuing this work** |
| **[NEXT_TASKS.md](NEXT_TASKS.md)** | **prioritised work queue with rationale** |
| [ARCHITECTURE.md](ARCHITECTURE.md) | how the pieces fit and why |
| [DECISIONS.md](DECISIONS.md) | engineering decisions and their evidence |
| [TELEMETRY.md](TELEMETRY.md) | event model, transport, no-fake-data rule |
| [PROVIDERS.md](PROVIDERS.md) | routing policy, Ox Alpha's real status |
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

**Ox Alpha Free is free for a limited time, not permanently.** OpenCode's own
documentation says so, so free status is a runtime-probed value and is never
rendered as unlimited.

**Ox Alpha is a reasoning model, and that changes the settings that work.** It
was measured spending 7,986–7,997 of an 8,000-token budget on *hidden*
reasoning, truncating the visible diff — 5 of 8 evolution iterations produced
nothing from ~130-second requests. The broker now detects `finish_reason=length`
and retries with a doubled budget, and `max_tokens`, the provider timeout and
the client timeout are tuned together. See [BENCHMARKS.md](BENCHMARKS.md).

**A listed model is not a working model.** Zen lists `deepseek-v4-flash-free`
and returns "Model is unavailable" for it. Discovery is therefore two-stage:
list, then smoke-test. Capabilities are probed too — which is why re-admitting
Ox Alpha to tool-using roles after
[anomalyco/opencode#44300](https://github.com/anomalyco/opencode/issues/44300)
was fixed upstream needed no code change at all.

## Licence

Apache-2.0, inherited from OpenEvolve. Upstream retains copyright over
`openevolve/`, `scripts/visualizer.py`, `configs/` and `examples/`.
