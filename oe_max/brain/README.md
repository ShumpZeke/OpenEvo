# BrainPort — provider-neutral intelligence boundary

OpenCode is the brain, OpenEvo is the search.

## Hard boundary

**OpenEvo owns:**
evolutionary search, mutation strategies, candidate generation requests, parent selection,
operator/bandit policies, archives, novelty, Pareto, failure memory, deterministic gates,
evaluation scheduling, benchmarks, candidate isolation, checkpoint/resume, experiment state,
lineage, budgets, promotion.

**OpenCode owns:**
provider, model, credentials, catalog, reasoning config, harness, coding/fs/shell tools,
session/context, permissions, model switching.

There is a hard architectural boundary between these responsibilities. No file in `oe_max/brain/`
may contain hardcoded model IDs, provider URLs, or API key env names. The only file allowed to
import the old provider stack is `legacy_adapter.py`, which is deprecated and will be deleted.

## Default: brain.mode = inherit

The model selected in OpenCode is the model OpenEvo uses. No double configuration.

Roles (EVOLVER/CRITIC/PLANNER etc.) are prompt policies, not providers:

```
EVOLVER    -> mutation-generation policy
CRITIC     -> adversarial-review policy
PLANNER    -> search-planning policy
ANALYST    -> experiment-analysis policy
ARCHITECT  -> architecture-mutation policy
RESEARCHER -> research policy
```

All policies run on the same inherited OpenCode model. Changing the model in OpenCode
requires zero OpenEvo source changes. The system works with any model OpenCode can expose.

Advanced role-specific overrides may exist behind an explicit config flag, but they are not
required and do not contaminate the core.

## Abstraction

```python
class BrainPort:
    async def generate(self, request: BrainRequest) -> BrainResponse: ...

request = BrainRequest(
    operation=Operation.MUTATE,
    objective="optimize foo",
    parent_code="...",
    mutation_strategy="LOCAL_OPTIMIZE",
    policy=PolicyMode.MUTATION_GENERATION,
    context={...},  # compact packet — not full history
)
```

The core requests intelligence through this port. The host fulfills it.

Implementations:
- `NullBrainPort` — deterministic stub for tests/benchmarks
- `LegacyBrainPort` — wraps the old Registry+Router (temporary)
- `StdioBrainPort` — delegates to the OpenCode TypeScript plugin over stdio JSONL (preferred)

## Capability negotiation, not model-name checks

Never:

```
if model == "x-preview-f-free": ...
if provider == "nvidia_nim": fallback to Y
```

Instead:

```
caps = await brain.capabilities()  # cached per run, not per generation
if caps.has(Capability.STRUCTURED_OUTPUT): ...
if caps.context_limit >= needed: ...
```

Capabilities: `text`, `tool-use`, `structured-output`, `vision`, `streaming`, `context limit`, `output limit`, `reasoning variants`, `cancellation`.

## Transport

The TypeScript plugin (`packages/opencode-plugin/`) owns the worker lifecycle
(start, health check, request, stream events, cancel, restart, shutdown, reconnect)
over a lightweight stdio JSONL duplex protocol (one JSON object per line).

```
plugin  --stdin-->  worker.py (JSONL RPC + brain_request)
plugin  <--stdout-- worker.py (JSONL RPC response + brain_response + events)
```

No HTTP microservice. The plugin calls `client.session.prompt` to fulfill
`brain_request`s with the user's selected OpenCode model.

## Evolution hardening (staged funnel)

```
candidate
  -> G0 validity (parse/apply)
  -> G1 dedup (exact / normalized / AST / structural)
  -> static checks
  -> impact analysis
  -> affected tests
  -> cheap benchmark
  -> fitness/novelty estimate
  -> full tests
  -> expensive benchmark
  -> semantic judge (only when required)
  -> archive/selection
```

Weak candidates die cheaply. LLM is not used for what deterministic tooling can do.

## Content-addressed caching

Identity: `base_sha + patch_hash + evaluator_version + benchmark_version + config_hash + toolchain`

Caches: equivalence, static analysis, impacted tests, test/benchmark outcomes (when safe), context retrieval, repo index.

See `cache.py` — bounded, evicts oldest, persists atomically.

## Budgets (provider-neutral)

```
max_brain_inflight, max_eval_workers, max_test_workers,
generation_budget, wall_clock_budget, token_budget, cost_budget,
candidate_budget, failure_budget
```

Generic adaptive backoff for transient failures. No hard-coded NVIDIA RPM contract.

## Isolation

`isolated_worktree()` — candidates evaluate in a git worktree or temp clone,
never in the user's active tree. Promotion is explicit (`promote_worktree_to_repo`).

## Checkpoint/resume

`checkpoint.py` — atomic JSON checkpoint per experiment (crash-safe via tmp+rename).
Records: experiment ID, goal, base SHA, config, seed, generation, candidates, lineage,
metrics, budgets, timestamps, host model meta, OpenCode/plugin/engine versions.
Provider identity is metadata only.

## Plugin tools

Exposed via OpenCode plugin tools (inherit model):

```
evolve/start, evolve/status, evolve/inspect, evolve/candidates,
evolve/apply, evolve/pause, evolve/resume, evolve/stop
```

Also `opencode.json` at repo root enables the plugin locally.

## Migration order (completed so far)

1. Tests + baseline benchmarks (this harness)
2. BrainPort extraction + NullBrainPort
3. LegacyBrainPort wrapping old stack (no hard break)
4. OpenCode plugin + stdio worker + brain bridge (inherit mode)
5. Policy modes replacing role->model matrices
6. Funnel, cache, isolation, budgets, checkpoint

Next: harden the full controller integration, then delete `oe_max/providers`, `oe_max/router`, `oe_max/limiter`, `control_plane/providers` after verification.

## Pin

OpenCode `1.18.23`, `@opencode-ai/plugin` `1.18.23`, `@opencode-ai/sdk` `1.18.23`.

## Verification

```
$ python -m pytest tests/brain -q                                  # 31 tests
$ powershell -File scripts/verify-brainport-acceptance.ps1         # 26 gates
$ python scripts/legacy_deletion_gate.py                           # may legacy go yet?
$ python benchmarks/benchmark_brainport.py --iterations 20 --seed 42
```

### What is and is not established

Every gate above runs against `NullBrainPort` or the stdio worker. That is
enough to prove the *structural* claim -- there is no model ID, provider URL or
API key env name anywhere in this package, and a test fails the build if one
appears -- so switching models genuinely cannot require a source change here.

It is not enough to prove the *behavioural* one. **No BrainPort run against a
live OpenCode host has been recorded.** Until one is, "the model you select in
OpenCode is the model OpenEvo uses" describes a wiring that is tested end to end
against a stub and never against a real host. The acceptance script reports that
as UNVERIFIED rather than counting the stub run as evidence.

The benchmarks are likewise stub-driven; `benchmarks/README.md` says what they
do and do not measure.

### Before deleting the legacy stack

`scripts/legacy_deletion_gate.py` is the check, and it currently reports
**BLOCKED**. The import scan is clean -- nothing in core reaches into
`oe_max/providers`, `oe_max/router`, `oe_max/limiter` or
`control_plane/providers` any more. But the default evolution path still runs
*through the broker over HTTP*: `configs/oe_max/evolution.yaml` points
`api_base` at `127.0.0.1:8787`, `scripts/start-broker.sh` launches it, and
`pyproject.toml` ships its console entry points.

None of that is an import, which is exactly why an import-only scan reported
"safe to delete" while the shipping path depended on it. Move that path onto the
BrainPort first; the gate will say so when it is done.
