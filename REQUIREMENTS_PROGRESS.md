# Requirements Progress — OpenEvolve MAX

Every normative requirement from `AUTONOMOUS_BUILD_PROMPT.txt` and
`SOURCE_SPEC.md`, mapped to where it lives, how it is tested, and its status.
Nothing is silently skipped: items not built say so and say why.

Legend — **DONE** built and tested · **PARTIAL** built with a stated gap ·
**BLOCKED** needs something unavailable · **DEFERRED** not built, reason given

---

## §2 Architecture

| Requirement | Status | Location | Evidence |
|---|---|---|---|
| Use the official OpenEvolve project | DONE | `openevolve/` | Both named repos resolve to the same commit `411fb59c` |
| Pin an exact commit and record it | DONE | `upstream/OPENEVOLVE_PIN.txt`, `UPSTREAM.json` | — |
| Do not rewrite working upstream features | DONE | `PATCH_SURFACE.md` | `openevolve/` byte-identical; 437 upstream tests pass |
| Reuse MAP-Elites, islands, migration, DB, cascade, checkpoints, prompts, visualizer | DONE | upstream, untouched | 437 tests |
| Build around/above upstream, not a lookalike | DONE | broker + `control_plane/` | OpenEvolve runs unmodified through the broker |

## §3 Ox Alpha primary

| Requirement | Status | Location | Evidence |
|---|---|---|---|
| Verify live provider data before finalising config | DONE | `providers/registry.py` | 64 models discovered live; `x-preview-f-free` confirmed |
| Ox Alpha as primary generator/reasoner | DONE | `router.DEFAULT_CHAIN` | Head of every chain |
| Never claim permanent availability | DONE | `ModelSpec.ephemeral_preview`, docs | Marked ephemeral; no permanence claimed anywhere |
| Model slugs never immutable constants | DONE | `providers/registry.py` | IDs are `ModelSpec` data; swapping is a config edit |
| Startup health checks detect missing/renamed routes | DONE | `/v1/oe-max/verify` | Two-stage: discover, then smoke-test |
| Clean failover | DONE | `router.chat` | 15 router tests |
| Record provider identity for every request | DONE | `ChatResult.to_log`, response `oe_max` block | Stamped on every response |
| Configurable, measurable allocation (not a hardcoded 70%) | DONE | `chain` + `stats_by_route` | Chain is data; per-route stats exposed |
| OpenRouter alternate Ox route | PARTIAL | `providers/registry.py` | Adapter configured; **unverified**, no key |

## §4 Broker

| Requirement | Status | Location | Evidence |
|---|---|---|---|
| Local OpenAI-compatible broker on 127.0.0.1:8787 | DONE | `oe_max/broker/app.py` | Live completion returned `BROKER_OK` |
| `GET /health`, `GET /v1/models`, `POST /v1/chat/completions` | DONE | same | Verified live |
| OpenEvolve points at the broker, not providers | DONE | `configs/oe_max/evolution.yaml` | Real 10-iteration run through it |
| Provider adapter interface | DONE | `providers/base.py` | One adapter, three providers |
| Broker owns credentials; sandboxes never receive keys | DONE | `ProviderAdapter.api_key` | Keys read only inside the broker process |
| FastAPI · uvicorn · httpx · asyncio · pydantic · structured logs | DONE | `oe_max/` | — |
| Never commit credentials | DONE | `.gitignore`, `.env.example` | `.env` ignored |

## §5 NIM hard limit

| Requirement | Status | Location | Evidence |
|---|---|---|---|
| 48 contract / 44 internal cap / 42 target | DONE | `limiter.RateLimiter` defaults | — |
| Every attempt through one global scheduler | DONE | one limiter per provider, shared | `test_many_concurrent_workers_share_one_budget` |
| Token bucket **and** exact rolling-window guard | DONE | `_wait_for_token`, `_wait_for_window` | Both implemented |
| Invariant: ≤44 starts in any contiguous 60 s | DONE | `limiter.py` | `assert_window_invariant` checks **every** window |
| Retries count | DONE | `acquire()` per attempt | `test_retries_count_against_the_budget` |
| No emergency bypass | DONE | no bypass path exists | — |
| Deterministic virtual-clock/property tests | DONE | `VirtualClock` | 17 tests: 1 worker, 20 workers, bursts, retries, cancellation, boundaries |
| Honour Retry-After; global slow-down; backoff + jitter | DONE | `penalise`, `RetryPolicy` | `test_penalty_slows_dispatch_globally` |
| Circuit breaker, cooldown, half-open probe | DONE | `health.CircuitBreaker` | `test_circuit_opens_and_removes_route_from_candidates` |
| Per-provider scheduler/health, not shared with Ox | DONE | `NullLimiter` for Zen | Zen has no stated contract |
| Restart/recovery behaviour | PARTIAL | in-process only | Limiter state is not persisted across a broker restart; a restart briefly forgets the window |

## §6 Model discovery / specialist routing

| Requirement | Status | Location | Evidence |
|---|---|---|---|
| Do not rely on stale NIM IDs | DONE | NIM `models` starts **empty** | Discovery-only by construction |
| Query current availability; smoke-test; record actual IDs | DONE | `registry.discover` + `verify` | Caught `deepseek-v4-flash-free` listed-but-dead |
| Capability registry | DONE | `ModelSpec.supports_tools/available` | Probed, not declared |
| Benchmark probes (validity, latency, request efficiency…) | PARTIAL | latency + tools + availability | Mutation-validity and fitness-delta probes not built |
| Different families for independent critique | DEFERRED | — | Needs the critic path (§8 V2) |

## §7 Search architecture

| Requirement | Status | Location | Evidence |
|---|---|---|---|
| Keep OpenEvolve's proven core | DONE | untouched | 437 tests |
| Mutation taxonomy (15 operator classes) | DONE | `search/operators.py` | All 15, with applicability rules |
| Adaptive operator selection, non-stationary bandit | DONE | `search/bandit.py` | Discounted Thompson; `test_discounting_lets_the_selector_change_its_mind` |
| Algorithm replaceable | DONE | `Selector` ABC + `SELECTORS` | 3 implementations |
| Seed Forge | DEFERRED | — | Not built |
| Heterogeneous island policies | DEFERRED | — | Upstream islands used as-is; policy layer not built |
| Adaptive model routing by operator/task-class | PARTIAL | route stats collected | Selection not yet conditioned on operator |
| Multi-offspring experiment | DEFERRED | — | See BENCHMARKS.md: high expected value given 130 s/request |

## §8 Candidate evaluation cascade

| Stage | Status | Location |
|---|---|---|
| G0 parse/syntax/imports/interface | DONE | `evaluation/gates.py::g0_validity` |
| G1 exact / normalized / AST / semantic dedup | DONE | `evaluation/gates.py::DedupIndex` (4 strengths) |
| E0 smoke · E1 cheap subset · E2 full evaluator | PARTIAL | upstream cascade used (`cascade_evaluation`) |
| V1 property / metamorphic / differential / randomized / hidden | DEFERRED | — |
| V2 symbolic / SMT / independent critic | DEFERRED | — |
| Champion re-verification protocol | DEFERRED | — |

25 gate tests. Weak candidates die at G0/G1 for microseconds — against a
measured ~130 s per generation, that is the cascade's whole economic argument.

## §9 Anti-reward-hacking

| Requirement | Status | Notes |
|---|---|---|
| Keys never reach candidates | DONE | Broker holds them; the engine gets only a local token |
| Isolated candidate execution | PARTIAL | `control_plane/sandbox/opencode.py` enforces the isolation boundary; container executors not built |
| Suspicious-jump verification | DEFERRED | Not built |
| Protect evaluator/hidden tests | DEFERRED | Needs the sandbox executors |

## §10–11 Archives and data layer

| Requirement | Status | Notes |
|---|---|---|
| MAP-Elites archive | DONE | Upstream, surfaced in the control plane |
| Lineage archive + full provenance | DONE | `control_plane/storage` — candidate, parent, model, provider, generation, island, metrics, seed, commit |
| Hall of Fame / Pareto / Novelty / Failure / Counterexample archives | DEFERRED | Not built |
| Operator & provider statistics | DONE | `bandit.snapshot`, `router.stats_by_route` |
| PostgreSQL / pgvector / Parquet / DuckDB | DEFERRED | SQLite + append-only NDJSON event log used instead; adequate at current scale, and the event log is the migration path |

## §12–13 Mathematics and meta-optimization

| Requirement | Status | Notes |
|---|---|---|
| NumPy / SciPy | DONE | Installed and used by the example |
| SymPy / mpmath / Z3 / Hypothesis / Lean 4 | DEFERRED | Not integrated |
| Safe meta-optimization, no live self-rewrite | DONE (by construction) | No self-rewriting path exists |

## §14–15 Observability and reproducibility

| Requirement | Status | Location |
|---|---|---|
| Terminal dashboard | DONE | `oe_max/dashboard.py` |
| NIM rolling-window count displayed | DONE | dashboard, with headroom bar |
| Operator / provider success, latency, errors, queue depth | DONE | dashboard + `/v1/oe-max/status` |
| Structured logs for later analysis | DONE | `control_plane` NDJSON event log |
| Preserve upstream visualizer | DONE | unmodified; `./run.sh classic` |
| Persist seeds, models, params, versions, Git SHA, pin | DONE | run provenance block |
| Exact prompts/templates persisted | PARTIAL | Prompts recorded when upstream attaches them to a candidate |

## §16 Benchmark requirement

| Requirement | Status |
|---|---|
| Stock vs MAX comparison | PARTIAL — harness and profiles exist (`run-evolution.sh --profile stock\|max`); a full multi-seed comparison has not been run |
| Ablations (10 listed) | DEFERRED — the bandit ablation is a one-line substitution; the rest need the unbuilt subsystems |
| Multiple seeds | DEFERRED |

See `BENCHMARKS.md` for what *was* measured.

## §18–20 Autonomy, non-interference, quality

| Requirement | Status | Notes |
|---|---|---|
| Operate autonomously on routine decisions | DONE | No questions asked; decisions recorded in `DECISIONS.md` |
| Never fabricate successful live API tests | DONE | NIM/OpenRouter explicitly marked unverified |
| Never place fake keys in the repository | DONE | `.env.example` has empty values |
| Confine work to this project | DONE | Only this repo modified |
| Do not repurpose the operator's OpenCode config | DONE | `control_plane/sandbox/opencode.py` redirects HOME/XDG; 9 tests |
| No cargo-culted Kubernetes/Kafka/Redis/vector DB | DONE | None added |

---

## Known defects

| Defect | Impact | Location |
|---|---|---|
| `control_plane/providers/doctor.py` probes with `urllib` | Cloudflare returns `403 error code: 1010` to urllib, so the old doctor reports Ox Alpha unavailable when it is healthy. The OE-MAX broker uses `httpx` and is unaffected. | `control_plane/providers/doctor.py` |
| Limiter state is in-process | A broker restart forgets the rolling window; a burst immediately after a restart could exceed the contract | `oe_max/limiter.py` |

## Blocked on credentials

`NVIDIA_API_KEY` and `OPENROUTER_API_KEY` are absent, so the NIM and OpenRouter
HTTP paths are **unverified**. Everything not requiring them is complete and
tested. To verify: add the keys to `.env` and run
`./scripts/verify-providers.sh`.
