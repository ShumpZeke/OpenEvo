# Feature Coverage Matrix

Status against every requirement in `EVOLUTION_SOURCE_OF_TRUTH.md`.

`PRESERVED` upstream behaviour intact · `NEW` built here · `ENHANCED` upstream
extended · `PARTIAL` built with stated gaps · `DEFERRED` not built, reason given
· `N/A` not applicable

## §1 Non-negotiable goals

| # | Goal | Status | Evidence |
|---|---|---|---|
| 1 | Fork the real OpenEvolve | PRESERVED | `411fb59c`; `openevolve/` byte-identical |
| 2 | Preserve upstream behaviour | PRESERVED | 437 upstream tests pass |
| 3 | Preserve the CLI | PRESERVED | `openevolve-run` unchanged in `[project.scripts]` |
| 4 | Preserve/expose classic visualizer | PRESERVED | `scripts/visualizer.py` unmodified; `/api/classic`; `./run.sh classic` |
| 5 | Add a Control Center | NEW | 20 views, React/TS |
| 6 | Structured telemetry at engine boundary | NEW | `telemetry/instrument.py`, 60+ event types |
| 7 | OpenCode sandbox as optional backend | PARTIAL | isolation enforced+tested; executors not built |
| 8 | Sandbox candidate execution | DEFERRED | boundary built; executors not — [../sandbox.md](../sandbox.md) |
| 9 | Pluggable OpenAI-compatible routing | NEW | `providers/` |
| 10 | NIM preferred, keyless Zen fallback | NEW | capability-aware; NIM leads every chain since 2026-08-27 — [../providers.md](../providers.md) |
| 11 | MAP-Elites/islands/candidates first-class | NEW | dedicated views + projections |
| 12 | Long runs, large histories | NEW | pagination, downsampling, canvas, indexes |
| 13 | Reproducibility | NEW | provenance block per run |
| 14 | Windows first-class | PARTIAL | `.ps1` scripts, no POSIX-only paths; **not executed on Windows** |
| 15 | One-command bootstrap/run/test | NEW | `bootstrap`/`run`/`dev`/`test` ×2 |
| 16 | No mock metrics or fake buttons | NEW | no frontend fixtures; unsupported controls disabled with reasons |
| 17 | Upstream-mergeable | NEW | empty patch surface |
| 18 | Verify fast-moving deps at install | NEW | provider doctor; OMO probed, not hardcoded |
| 19 | Autonomy inside project, host preserved | NEW | isolation refuses operator paths |
| 20 | Research console, not SaaS | NEW | dense dark console |

## §2 Upstream baseline preserved

Controller, MAP-Elites database, islands + migration, population/archive config,
feature dimensions/bins, LLM config, ensembles, evaluator + cascade, parallel
evaluation, novelty/duplicate controls, artifacts, checkpoint/resume, prompt
logging, CLI flow, web visualizer — **all PRESERVED**, `openevolve/` unmodified,
437 tests green.

## §5–6 Sandbox & candidate bundle

| Item | Status |
|---|---|
| Isolation model (config/state/cache/sessions/logs/ports) | NEW — enforced by filtered env |
| Forbidden-path enforcement | NEW — `_assert_safe`, 9 tests |
| Preferred runtime order (container → project-local → binary) | NEW — `preflight()` |
| OMO compatibility rule (no stale package name) | NEW — probes 3 commands |
| Modes A–D (direct / agent-realized / harness / hybrid) | DEFERRED — designed, documented, not built |
| CandidateBundle schema | DEFERRED — code candidates only |

## §8 Telemetry

All families in section 8 defined and emitted where the corresponding subsystem
runs: experiment, generation, candidate, population/archive, island, migration,
MAP-Elites, model, evaluator, checkpoint, resource, system, telemetry health,
control commands. Sandbox/OpenCode/OMO families are **defined and project
correctly** but unpopulated until the executors exist.

Base schema fields all present, plus `pid` (runs span processes). Redaction
before persistence: NEW.

## §9–22 Control Center

| Screen | Status |
|---|---|
| Overview / command centre | NEW |
| Evolution graph (lineage) | NEW — canvas, deterministic layout |
| Candidate Inspector (10 tabs) | NEW — incl. LCS diff vs parent |
| MAP-Elites Lab | NEW — selectable axes, generation scrubber, island scoping |
| Islands / migration | NEW — incl. migration matrix |
| Experiment Builder | PARTIAL — simple/advanced/raw; engine settings live in YAML by design |
| Run controls | NEW — real; unsupported ones disabled with reasons |
| Models / providers | NEW — route table with exclusion reasons |
| Model request inspector | NEW |
| Evaluators | NEW |
| Agent Sandbox | PARTIAL — status honest, no runs to show |
| Checkpoints | NEW — list/resume/delete, disk-authoritative |
| Activity | NEW |
| Metrics / resource timeline | NEW |
| Errors | NEW — grouped by cause |
| Traces | NEW — per-candidate span timeline |
| Run comparison | NEW |
| Global search + palette | NEW — FTS5, Ctrl/Cmd+K |
| System health | NEW — incl. isolation report |
| Telemetry self-health | NEW |
| Classic visualizer bridge | PRESERVED |
| Settings | NEW |
| Alerting | DEFERRED — table + thresholds designed, no evaluator |
| History / replay | PARTIAL — full event history + MAP-Elites scrubber; no generation-by-generation replay |

## §16–17 Providers

Default routing, reliability router, provider profiles, health-aware routing,
concurrency, backoff, circuit breaking, free-status modelling, cost metadata,
model observability — **NEW**. Live probes against Zen/NIM **unverified** (no
credentials available).

## §23–26 Storage, API, performance, security

Logical entities, event/projection split, control/query/stream split, SSE,
async bounded emission, batching, back-pressure-by-drop-and-count, server-side
filtering and downsampling, virtualised rendering, retention, secret broker by
reference, redaction pipeline — **NEW**.

## §27 Upstream compatibility

Adapter layer, runtime hooks, isolated frontend package, compatibility tests,
`../patch-surface.md`, `../upstream-sync.md` — **NEW**. Patch surface empty.

## §35 Acceptance criteria

| # | Criterion | Status |
|---|---|---|
| 1 | Original CLI runs | ✅ preserved, tests green |
| 2 | Classic visualizer accessible | ✅ `/api/classic`, `./run.sh classic` |
| 3 | Upstream example completes through the fork | ✅ `function_minimization`, 28 candidates |
| 4 | Checkpoint created and resumed | ✅ 6 checkpoints; resume wired and exercised |
| 5 | Graph reflects real lineage | ✅ 27 edges from real parent ids |
| 6 | MAP-Elites reflects real cells | ✅ 21 cells across 3 islands |
| 7 | Island view reflects real assignment/migration | ✅ real populations, diversity, migrants |
| 8 | Inspector shows real code/diff/prompt/eval | ✅ code+diff+eval real; prompt only when upstream links it |
| 9 | Model table identifies the handling provider | ✅ per-request provider/model/latency/tokens |
| 10 | Evaluator table shows real results | ✅ 23 evaluations |
| 11 | No fabricated production chart | ✅ no frontend fixtures |
| 12 | Controls invoke real backend operations | ✅ verified start/stop/checkpoint |
| 13 | Large event streams stay responsive | ⚠️ designed + bounded; verified at tens, not tens of thousands |
| 14 | Secrets redacted from logs | ✅ pre-persistence, 12 tests |
| 15 | OpenCode sandbox runs a benchmark candidate | ❌ **not implemented** — reported disabled |
| 16 | OMO optional and dynamically verified | ✅ probed, not hardcoded |
| 17 | OMO failure doesn't break core | ✅ absence is a normal path |
| 18 | Candidate can't modify unrelated host files | ⚠️ isolation enforced for OpenCode; native evaluator keeps upstream's model ([SECURITY.md](../../SECURITY.md)) |
| 19 | Windows bootstrap on a clean system | ⚠️ written, **not executed on Windows** |
| 20 | Linux bootstrap on a clean system | ✅ this environment |
| 21 | Tests document what was validated | ✅ [../testing.md](../testing.md) |
| 22 | Usable at 1080p and ultrawide | ✅ verified 1920×1080; fluid grid |
| 23 | Upstream merge documented | ✅ [../upstream-sync.md](../upstream-sync.md) |
| 24 | No core feature silently removed | ✅ this matrix + 1453 passing tests |
| 25 | Doctor resolves the primary first | ✅ NIM at priority 0, probed first |
| 26 | Automatic fallback when unavailable | ✅ circuit breaker + chains, tested |
| 27 | Never labels a free route permanently unlimited | ✅ three-valued status, asserted in tests |

**22 met · 4 partial (13, 18, 19 — verified in principle but not at scale/on
Windows/for native evaluation) · 1 not met (15).**

## Deferred, with reasons

| Item | Why |
|---|---|
| Sandbox executors | Largest remaining subsystem; isolation boundary built first so it cannot be bolted on unsafely |
| OMO orchestration | Depends on the executors; detection built, no install available to verify against |
| Alert engine | Schema and thresholds designed; needs a rules evaluator |
| Generation-by-generation replay | Event history and MAP-Elites scrubber exist; full scrubbing across all views not built |
| Agent-config candidate types | Requires the executors to evaluate them |
| Pause/resume in place, fork-from-candidate | Upstream cannot support; reported unsupported with reasons rather than faked |
