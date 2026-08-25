# Evolution

A production fork of [OpenEvolve](https://github.com/codelion/openevolve) that
keeps the evolutionary engine exactly as upstream ships it and adds a real-time
control plane around it: a browser Control Center, typed telemetry, a query and
control API, health- and capability-aware model routing, and an isolated agent
sandbox.

Forked from upstream `411fb59c` (v0.3.2, Apache-2.0). The engine is
**byte-identical to upstream** — see [PATCH_SURFACE.md](PATCH_SURFACE.md).

---

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
- **Agent sandbox** — OpenCode as an optional evaluation backend, in an
  environment that cannot reach the operator's own OpenCode installation.
- **Everything upstream still works** — the original CLI, configs, examples,
  checkpoint/resume, and the classic visualizer are preserved and reachable.

## Quick start

```bash
./bootstrap.sh          # environment, deps, storage, UI build, doctor checks
./run.sh                # Control Center → http://127.0.0.1:8000
```

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
scripts/            upstream visualizer + a local test provider
control_plane/
  telemetry/        event model, redaction, bus, engine instrumentation
  storage/          SQLite event log + derived projections
  api/              control, query and SSE streaming
  providers/        profiles, runtime doctor, health/capability router
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
| Control plane | **81 passed** |
| Web typecheck | clean |

The upstream suite runs first: a change that breaks it is a regression in the
fork, not merely a control-plane bug.

## Documentation

| | |
|---|---|
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

## Two things worth knowing up front

**Ox Alpha Free is free for a limited time, not permanently.** OpenCode's own
documentation says so, so Evolution treats free status as a runtime-probed
value and never renders it as unlimited.

**Ox Alpha currently fails on any request carrying tools.** Verified against
[anomalyco/opencode#44300](https://github.com/anomalyco/opencode/issues/44300).
Plain completions work, so it remains the preferred route for OpenEvolve's
mutation calls; agent roles that need function calling fall back automatically,
and the Models page shows exactly why. The provider doctor re-probes this, so
the moment it is fixed upstream the router can promote it back with no code
change.

## Licence

Apache-2.0, inherited from OpenEvolve. Upstream retains copyright over
`openevolve/`, `scripts/visualizer.py`, `configs/` and `examples/`.
