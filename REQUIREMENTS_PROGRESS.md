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
| Restart/recovery behaviour | DONE | `limiter.RateLimiter(state_path=…)` | Attempt starts persist to `.evolution/nim.window`; those still inside the window are restored on start. Corrupt/aged state discarded, restore capped. 6 tests |

## §6 Model discovery / specialist routing

| Requirement | Status | Location | Evidence |
|---|---|---|---|
| Do not rely on stale NIM IDs | DONE | NIM `models` starts **empty** | Discovery-only by construction |
| Query current availability; smoke-test; record actual IDs | DONE | `registry.discover` + `verify` | Caught `deepseek-v4-flash-free` listed-but-dead |
| Capability registry | DONE | `ModelSpec.supports_tools/available` | Probed, not declared |
| Benchmark probes (validity, latency, request efficiency…) | DONE | `oe_max/route_quality.py`, `control_plane/analysis/route_quality.py` | Mutation validity, duplicate rate and fitness delta measured per route from a run's own telemetry, in three efficiency views (per request, per second, per 1k tokens). Live: nemotron-3-ultra-free over 12 attempts — 100% valid, 0 duplicates, 58% improved, 83.6 s mean |
| Different families for independent critique | DEFERRED | — | Needs the critic path (§8 V2) |

## §7 Search architecture

| Requirement | Status | Location | Evidence |
|---|---|---|---|
| Keep OpenEvolve's proven core | DONE | untouched | 437 tests |
| Mutation taxonomy (15 operator classes) | DONE | `search/operators.py` | All 15, with applicability rules |
| Adaptive operator selection, non-stationary bandit | PARTIAL | `search/bandit.py` | The selector is built and tested (discounted Thompson; `test_discounting_lets_the_selector_change_its_mind`) but is **not** what picks operators in a live run — selection is uniform random until per-operator reward exists to learn from. Marked PARTIAL rather than DONE because the algorithm working is not the same as it being in the loop |
| Algorithm replaceable | DONE | `Selector` ABC + `SELECTORS` | 3 implementations |
| Seed Forge | PARTIAL | `oe_max/search/seed_forge.py` | Builds a starting population from one seed with **no model requests**: literal and effort-dial scaling, then the same G0/G1 gates a mutated candidate faces. Measured on the shipped example: 7 valid distinct variants, scores 0.769–1.478 against the seed's 1.423, two of them better. A run can now start from the forged population (`OE_MAX_SEED_FORGE=N`), spread across islands so migration has something to exchange from the first generation. PARTIAL because the variants that won did so by running the search harder — trading local compute for score, not a better algorithm — and because whether starting from a population beats starting from one program is unmeasured |
| Heterogeneous island policies | PARTIAL | `oe_max/search/policies.py` | Four policies (exploit/explore/balanced/refine) as preferences over `Operator.disruption`, assigned round-robin and applied to operator selection per island (`OE_MAX_ISLAND_POLICIES`). Measured over 3,000 draws: exploit 0.314, refine 0.458, balanced 0.566, explore 0.712 against an unweighted 0.571. PARTIAL because whether the heterogeneity *helps* is unmeasured — it needs a run with policies on against one with them off |
| Adaptive model routing by operator/task-class | PARTIAL | `instrument.install_operator_hook`, `route_quality.operator_breakdown` | Mutations are labelled with an operator class and per-operator, per-route quality is measured (verified live: 10 distinct operators over 12 iterations). Selection is uniform random, not yet the bandit — there is no per-operator reward to learn from until this has run |
| Multi-offspring experiment | DONE | `control_plane/telemetry/multi_offspring.py` | Benchmarked on a real provider: **2.73 distinct candidates per request against 1.00**, 9% duplicates. The local stub's 69% duplicate rate was its own fixed pool, not the feature. Per *second* the gain is only 1.11x, because each request became 2.5x slower — see BENCHMARKS for which number applies when. One repeat per arm |

## §8 Candidate evaluation cascade

| Stage | Status | Location |
|---|---|---|
| G0 parse/syntax/imports/interface | DONE | `evaluation/gates.py::g0_validity` |
| G1 exact / normalized / AST / semantic dedup | DONE | `evaluation/gates.py::DedupIndex` (4 strengths) |
| E0 smoke · E1 cheap subset · E2 full evaluator | PARTIAL | upstream cascade used (`cascade_evaluation`) |
| V1 property / metamorphic / differential / randomized / hidden | DONE | `oe_max/verification/` | Property, metamorphic and randomized checks plus generic ones needing no task knowledge (loads, deterministic under seed, finite score). Verified by feeding it four programs that *cheat* — fabricated value, hard-coded answer, NaN, non-deterministic score — all caught. `examples/function_minimization/verification.py` is a working spec |
| V2 symbolic / SMT / independent critic | DEFERRED | — |
| Champion re-verification protocol | DONE | `control_plane/telemetry/verification_hook.py` | Every new champion is verified before the run keeps optimising around it; verified once per candidate, so a champion re-confirmed is not re-run |

25 gate tests. Weak candidates die at G0/G1 for microseconds — against a
measured ~130 s per generation, that is the cascade's whole economic argument.

## §9 Anti-reward-hacking

| Requirement | Status | Notes |
|---|---|---|
| Keys never reach candidates | DONE | Broker holds them; the engine gets only a local token |
| Isolated candidate execution | PARTIAL | `oe_max/execution/` — subprocess backend with real POSIX ceilings (CPU, address space, file size, process count, wall clock), a fresh working directory per run, an environment allowlist and process-group kill on timeout. Container backend implemented (`--network none`, read-only root, all capabilities dropped) but **unverified here**: docker is installed and its daemon is unreachable, so the probe correctly reports it unavailable. PARTIAL because the subprocess backend does not stop network access, filesystem reads, or importing packages installed in the interpreter — all three are declared in `describe_backends()` rather than papered over |
| Suspicious-jump verification | DONE | `verification/suspicion.py` | Modified z-score on the run's own improvement history, median/MAD rather than mean/σ so one outlier cannot desensitise the detector against the next. Emits `candidate.suspicious` and triggers V1 |
| Protect evaluator/hidden tests | PARTIAL | The candidate no longer runs in the evaluator's process (`OE_MAX_SANDBOX_EVAL=1`): separate process, resource ceilings, environment allowlist, fresh working directory. PARTIAL because the subprocess backend cannot stop filesystem reads, so a candidate can still *read* a hidden test file — only the container backend closes that, and it is unverified here |

## §10–11 Archives and data layer

| Requirement | Status | Notes |
|---|---|---|
| MAP-Elites archive | DONE | Upstream, surfaced in the control plane |
| Lineage archive + full provenance | DONE | `control_plane/storage` — candidate, parent, model, provider, generation, island, metrics, seed, commit |
| Hall of Fame archive | DONE | `archives.HallOfFame` — keeps deposed champions, not just a top-N list |
| Pareto archive | DONE | `archives.ParetoArchive` — non-dominated set; crowding-aware trimming keeps the front's extremes |
| Novelty archive | DONE | `archives.NoveltyArchive` — k-NN behaviour distance |
| Failure archive | DONE | `archives.FailureArchive` — indexed by reason/operator, with a cheap `already_failed` pre-check and capped, de-duplicated prompt context |
| Counterexample DB | DONE | `verification/counterexamples.py` | Every verification failure is stored, deduplicated by check-plus-input, evicted by how many candidates it has caught. `prompt_context()` is what COUNTEREXAMPLE_REPAIR and ADVERSARIAL_REPAIR need to be offered at all |
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
| Ablations (10 listed) | PARTIAL — `scripts/ablation.sh` runs each optional behaviour on and off against a shared baseline and reports area under the best-so-far curve *per request*. Four arms exist today (operators, island policies, multi-offspring, verification); the remaining spec arms need the unbuilt subsystems, and the bandit arm needs the bandit to be in the loop |
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
| ~~`doctor.py` probes with `urllib`~~ | **FIXED.** Now probes with `httpx` (urllib kept as a last-resort fallback, and a 1010 from it is reported as an inconclusive transport block rather than a provider fault). Verified live: `zen-ox-alpha-free` reports `available=True, caps=[chat, tools]` with no key set. | `control_plane/providers/doctor.py` |
| Limiter state is in-process | A broker restart forgets the rolling window; a burst immediately after a restart could exceed the contract | `oe_max/limiter.py` |

## Blocked on credentials

`NVIDIA_API_KEY` and `OPENROUTER_API_KEY` are absent, so the NIM and OpenRouter
HTTP paths are **unverified**. Everything not requiring them is complete and
tested. To verify: add the keys to `.env` and run
`./scripts/verify-providers.sh`.
