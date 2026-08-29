# Next Tasks

A prioritised, self-contained work queue. Each task states why it is worth
doing, where to start, how to know it worked, and what would make it a bad
idea. Ordered by expected value, not by convenience.

Read `handoff.md` first — especially §3, the traps.

---

## T0-noise — Make the task's score comparable between runs — DONE (2026-08-29)

Solved by `benchmarks/tasks/fn_min_seeded`, a fork-owned copy of the function
minimization task whose evaluator fixes the random draws. `examples/` is
upstream and byte-identical, so the deterministic evaluator lives beside it
rather than replacing it.

Measured on the unchanged seed program, twenty evaluations each:

| evaluator | spread |
|---|---|
| `examples/function_minimization` | **0.3857** (1.0330–1.4188) |
| `benchmarks/tasks/fn_min_seeded` | **0.0000** (1.406051 every time) |

Reproduce with `check_determinism.py` in that directory; it exits non-zero when
the evaluator under test is not deterministic, so it can gate a change.

The metric names and weights are deliberately identical to upstream's, so the
meaning did not silently change — but scores from the two are not
interchangeable. Fixing the draws makes this one particular sample of the
upstream metric rather than an estimate of its mean, and the README says so.

### What it found on first use

`compare.py` prints the ten trials behind the aggregate. A local-refinement
variant lands closer to the global minimum on **9 of 10 seeds**, about five
times closer — and scores *lower* (1.3564 vs 1.4061). On the tenth seed it
refines into the wrong basin at distance 3.99, and `combined_score` averages
distances, so one trapped trial outweighs nine near-perfect ones.

So the task's metric is outlier-dominated: it prefers a program that is mediocre
everywhere to one that is excellent nine times in ten. That is a property of
upstream's metric, recorded rather than fixed — changing the weights would make
every recorded number incomparable. `tests/evolution/test_seeded_benchmark.py`
pins the finding so the README cannot rot.

### Follow-on

The unanswerable comparisons are now answerable and worth actually running:
27B vs 0.6B on quality, and whether the shipped prompt's rules arm helps or only
shortens.

## T0a — Run local mode against a real local LLM — DONE (2026-08-29)

Two complete runs against models on this machine, through Ollama. Both produced
real, reproducible improvements, so the claim "no local model has generated a
mutation here" is retired.

| model | iterations | wall | best program | score |
|---|---|---|---|---|
| `qwen3:0.6b` | 30 | ~3 min | the seed's search at budget 2000 | 1.4513 |
| `qwen3:0.6b` again | **300** | ~30 min | nothing — zero new bests | 1.4061 (the seed's) |
| Qwen3.5-27B (tuned) | 19 of 30, stopped; best at 6 | ~180 s/iteration | Differential Evolution, adaptive F/CR | 1.4987 |

Against the seed's 1.4061, on `benchmarks/tasks/fn_min_seeded` — the seeded task
from T0-noise, without which neither number would mean anything.

The 27B's gain is almost entirely algorithmic: held at the seed's budget of 1000
it still scores 1.4962, so +0.090 is the algorithm and 0.0025 is the budget. The
0.6B's whole gain was the budget. `docs/local-tuning.md` has the full comparison.

### What this run surfaced, which was the more valuable half

Both fixes are committed; they are recorded here because each was invisible
until a real model ran.

* Pointing `api_base` straight at Ollama loses the broker adapter's
  `reasoning_effort: "none"`, and a local reasoning model then answers entirely
  in `message.reasoning` with an empty `content`. Every iteration fails with "No
  valid diffs found in response" — 308.7 s per iteration to produce nothing,
  against 99.3 s to produce an applicable diff. Now set in the config as well as
  the adapter.
* On Windows the run log **deletes** records it cannot encode rather than
  mangling them. Upstream marks a new best with a star; cp1252 cannot encode it,
  `logging` raises, and the record is dropped — 0 bytes. A run that found a new
  best could produce a log that never says so. `PYTHONIOENCODING=utf-8` is now
  set on all three launch paths.
* The seeded task's trial timeout did not time out. `with ThreadPoolExecutor(...)`
  joins the worker on exit, so a 12 s trial under a 5 s limit returned after
  12.0 s — and the executor's non-daemon threads then blocked interpreter exit.

### Through the broker as well

Closed the same day. A broker started with `OE_MAX_LOCAL_ONLY=1` discovered
twelve local models, built its chain from that listing, and served a complete
run:

    ollama/qwen3:0.6b   10 requests, 10 ok, 0 errors, 23,196 tokens, 1055 ms mean

The route is the one `OE_MAX_LOCAL_MODELS` named, so the operator preference
reaches the chain and not only the adapter. Broker overhead against calling
Ollama directly is **+2 ms** at the median, n=10 per arm alternated.

So discovery, materialisation, chain construction, routing, the offline
guarantee and a full evolution cycle are now proven against a real local model
rather than against `scripts/local_provider.py`.

## T0 — Record one live BrainPort run against a real OpenCode host

**Priority:** highest. **Effort:** small to run. **Blocked by:** an OpenCode host.
**Status:** everything is built and nothing has been observed.

### Why

`oe_max/brain/` is 2,700 lines, 31 tests and 26 passing acceptance gates, and
**every one of them runs against `NullBrainPort` or the stdio worker.** The
structural claim is genuinely proven — no model ID, provider URL or key env name
exists in the package and a test enforces it. The behavioural claim is not
proven at all: nobody has watched a real OpenCode model answer a `BrainRequest`.

Until that happens the whole second path is an untested integration wearing a
green test suite, which is the most expensive kind of thing to leave lying
around — every later change gets built on an assumption nobody checked.

### How

```bash
npm --prefix packages/opencode-plugin install && npm --prefix packages/opencode-plugin run build
opencode   # with opencode.json loaded; select any model
# then drive evolve_start / evolve_status / evolve_candidates from the plugin
```

### Done when

A run is recorded with the host model named in the checkpoint, and
`scripts/verify-brainport-acceptance.ps1` can move "a real OpenCode model serves
a full evolution run" out of UNVERIFIED. Write the numbers into
`benchmarks/README.md` next to the stub ones, clearly separated.

### Careful

The stub cannot tell you about truncation, latency, or whether a real model's
output survives `extract_diffs`. Those are exactly the failures §3.2 and §4c
cost days to find on the shipping path, and none of that experience transfers
automatically — the BrainPort has its own parser and its own funnel.

---

## T0b — Move the shipping path onto BrainPort, then delete legacy

**Priority:** high, but strictly after T0. **Effort:** large.

`scripts/legacy_deletion_gate.py` reports **BLOCKED** and lists 13 runtime
couplings. The import scan is clean; the dependency is over HTTP, which is why
an import-only scan previously reported "safe to delete" while the entire
default path ran on the broker.

Do not delete anything until the gate reports READY. And do not make the gate
report READY by editing the gate.

---

## T1 — Route quality per role, now that roles exist

**Priority:** highest. **Effort:** small — the machinery is built.
**Blocked by:** nothing. **Status:** the question changed; re-ask it.

### What happened to the old T1

The old T1 asked "does Ox Alpha produce better mutations than
`nemotron-3-ultra-free`, and by how much per second?" **That question is dead.**
Ox Alpha was withdrawn from OpenCode Zen on or before 2026-08-26 — absent from
`/models`, and answering `ModelError: Model x-preview-f-free is not supported`.
Two attempts to run that experiment had already been called off because the
route was too slow and too unreliable to reach the minimum sample size. It is
now impossible rather than merely expensive.

Keep the lesson, not the arm: **the experiment was blocked on the route, not on
the harness**, and the route then ceased to exist. Check a route is alive and
above ~25% success before pinning an arm to it (`./scripts/verify-providers.sh`).

### The question worth asking now

Routing is per role (`oe_max/roles.py`), and the role assignment is **argued
from a two-word probe, not measured on real work**:

| route | probe latency | reasoning tokens | assigned to |
|---|---|---|---|
| `nemotron-3-ultra-free` | 3.3 s | 39 | reasoner, coder |
| `hy3-free` | 2.1 s | 43 | — |
| `laguna-s-2.1-free` | 1.6 s | **0** | judge, fast |
| `nemotron-3.5-lightning-free` | 7.6 s | 64/64 truncated | — |

The reasoning-token argument is sound and the *conclusion is untested*. Two
things follow, and they are separate experiments:

1. **Is laguna good enough to judge?** A cheap judge that ranks badly is worse
   than an expensive one, because it corrupts the score rather than the
   candidate — much harder to notice than a broken diff. Compare
   `use_llm_feedback` runs with `evaluator_models` pinned to laguna against the
   same pinned to nemotron, and look at whether the *ranking* agrees, not at
   throughput.
2. **Is hy3 a better reasoner than nemotron?** It probed faster (2.1s vs 3.3s)
   with comparable reasoning tokens, and nothing has measured its mutation
   quality at all. Under a real run the primary averaged **90 s** per request,
   so probe latency is clearly not predictive and this needs a real arm.

### How to run it

```bash
./scripts/start-broker.sh
./scripts/route-experiment.sh \
    --routes nemotron-3-ultra-free,hy3-free \
    --iterations 12 --repeats 3
```

Three repeats per arm is the point: `MIN_ATTEMPTS_FOR_COMPARISON` is 20 and a
12-iteration run does not get there alone.

### Done when

You can answer "which free Zen route produces the best mutations per second,
and does laguna rank candidates the same way nemotron does?" with numbers from
≥3 runs per arm, and the verdict is not "insufficient evidence".

### Careful

Read all three efficiency views before concluding: `improvement_per_request`
matters under a rate contract, `improvement_per_second` when wall-clock is the
constraint, `improvement_per_1k_tokens` when someone is paying. The same two
routes can rank differently under each, and that is a result rather than a
contradiction.

And do not promote a route to primary on latency alone — that was the standing
instruction when the operator had chosen a primary, and it survives the
operator's choice being withdrawn.

---

## T1b — ~~Verify NVIDIA NIM for real~~ (DONE 2026-08-28)

**Priority:** high. **Effort:** trivial once a key exists. **Blocked by:** a
credential.

**Done.** A key was supplied and all nine were probed; five serve and four do
not. Results and the corrected ids are in docs/project/handoff.md §4i. The headline: two
transposed words in `nemotron-3-nano-30b-a3b` separated a working model from a
404, with both spellings in the catalogue.

What remains open from this task is the rate limiter (below) and the other
thirteen providers, each of which needs only a key.

```bash
echo "NVIDIA_API_KEY=nvapi-..." >> .env
./scripts/start-broker.sh
./scripts/verify-providers.sh
```

Then check three things that have never been observed:

1. Which of the nine actually serve, and which support tools.
2. Whether the 44-per-60s limiter holds against the real endpoint. The
   invariant has 17 property tests on a virtual clock and has never met NIM.
   Watch the rolling-window gauge on `./scripts/dashboard.sh`.
3. Whether NIM's free credits are a recurring allowance or a one-off. If they
   are one-off, the role chains should keep NIM behind the keyless Zen routes
   permanently rather than only while unverified.

The same applies to the thirteen catalogue providers, which need only a key
each — their model ids are discovered, so nothing else is configured.

---

## T1c — Does the bandit beat uniform random?

**Priority:** high. **Effort:** small — the arm is registered.
**Blocked by:** nothing.

The operator bandit is now wired end to end (`OE_MAX_OPERATOR_BANDIT=1`,
docs/project/handoff.md §4b-bandit) and **off by default because nobody has measured whether it
helps**. That is the only thing standing between it and being a default.

```bash
./scripts/start-broker.sh
./scripts/ablation.sh --arms operators,operator_bandit --repeats 3
```

Compare against the `operators` arm, not the baseline. The bandit acts through
operator steering, so measuring it against a plain baseline measures steering
and selection together and cannot say which one paid.

### Careful

Two things make this arm harder to read than the others, and both push toward
*more* repeats rather than fewer:

- **A short run barely leaves exploration.** Twelve iterations gives the bandit
  about a dozen observations spread over fifteen arms. Thompson sampling will
  still be sampling widely at that point, so a null result is evidence about
  short runs and not about the bandit. If the answer comes back "no
  difference", the honest next step is a longer run, not a conclusion.
- **It is the one arm that is not reproducible.** Selection depends on rewards
  from earlier iterations, so a rerun with the same seed diverges the moment a
  score differs. Do not treat run-to-run variation here as a bug.

### Done when

You can say whether reward-driven operator selection beats uniform on
improvement-per-request, with ≥3 runs per arm — or state plainly that a run of
this length cannot tell, which is also a result.

---

## T2 — Multi-offspring per request (spec §7F)

**Priority:** built, needs a real-provider benchmark. **Effort:** small.
**Status:** implemented and verified locally; the number that matters is not
measured yet.

### What is built

`OE_MAX_MULTI_OFFSPRING=3` asks each request for N alternatives and turns the
extras into ordinary candidates — same MAP-Elites placement, same novelty gate,
same telemetry. `control_plane/telemetry/multi_offspring.py`.

Measured on a local run: raw yield **1.00 → 2.42 candidates per request**, but
**distinct** yield only 0.58 → 0.75, because 69% of the extra output was the
same program again. The mechanism works; whether it *pays* is the open
question, and the raw number is the one that will mislead you.

### What is not measured

**Whether a real model's alternatives are actually different.** The local
provider draws from a fixed pool of five mutations, so its duplicate rate says
nothing about a real one. The failure mode this feature has is precisely three
near-identical alternatives collapsing to one AST hash — throughput that is not
real — and only a real provider can show it.

```bash
./scripts/start-broker.sh
OE_MAX_MULTI_OFFSPRING=3 ./scripts/run-evolution.sh --iterations 12
```

Then compare against the same run at N=1. The numbers to read:

| | where |
|---|---|
| candidates per request | `candidates` ÷ mutation `model_requests` |
| *useful* candidates per request | exclude `candidate.rejected` events |
| distinct code hashes | `SELECT COUNT(DISTINCT code_hash)` |
| whether siblings ever win | `is_best` on a row with `multi_offspring` |

### Done when

Candidates-per-request and *useful* candidates-per-request are measured against
the N=1 baseline over ≥3 seeds on a real provider.

### Careful

The spec says do not enable globally until benchmarked, which is why it is
opt-in. Watch the duplicate rate, not the raw count: three alternatives that
collapse to one AST hash are one candidate that cost a request-and-a-half.

Watch truncation too. Asking for three alternatives triples the response
length, and a reasoning model that spends 7,986 of an 8,000-token budget on
hidden reasoning has nothing left. `MAX_OFFSPRING` is capped at 5 for that
reason; if truncation rises, lower N before raising `max_tokens` (see
docs/gotchas.md — that is one coupled setting, not three).

---

## T2b — Run the ablations against a real provider

**Priority:** high. **Effort:** small to run, hours to wait.
**Blocked by:** nothing. **Status:** harness built and smoke-tested; no result.

### Why

Four features are marked PARTIAL in `requirements.md` for the same
reason — they are built, gated and unmeasured. `scripts/ablation.sh` settles
each one against a shared baseline.

```bash
./scripts/start-broker.sh
./run.sh
./scripts/ablation.sh --config configs/oe_max/evolution.yaml \
    --arms operators,island_policies,multi_offspring --repeats 3 --iterations 12
```

### The trap

**Do not run this against the local test provider and believe the result.** It
replays a fixed pool of five diffs and never reads the prompt, so the
`operators` and `island_policies` arms cannot show an effect no matter how well
they work. A smoke test produced a 1.75x "improvement" from the `operators` arm
that way — pure variance over eight requests. Only `multi_offspring` is
answerable against the stub, because the provider does honour a request for N
alternatives.

### What one round already showed

Recorded in BENCHMARKS: `multi_offspring` gives **2.73 distinct candidates per
request against 1.00** on a real provider — and only **1.11x per second**,
because each request became 2.5x slower. `operators` gives 1.071x on
area-under-curve under matched conditions. `seed_forge` timed out with 5
requests and showed nothing.

One repeat per arm, so none of it is settled.

### Interleave the repeats

The provider drifts *during* the experiment — nemotron went 77% → 48% success
with its latency doubling, in one afternoon.

**This needs no flag: pass `--repeats 3`.** The harness already runs a fresh
baseline before each round of arms rather than all baselines first, so repeats
are interleaved by construction and drift lands on both sides. With
`--repeats 1` there is nothing to interleave, which is exactly why the first
recorded ablation's latency figure carries an ambiguity it cannot resolve.

Watch latency, not success rate, when judging conditions: the broker retries,
so a run's recorded success rate reads 100% while the provider is at 48%. The
cost lands in latency.

### Done when

Each arm has ≥3 interleaved repeats against the broker and a verdict that is
not "insufficient evidence" and carries no drift caveat. Then move the matrix
rows to DONE *with the numbers*, or to a stated negative result, which is
equally worth having.

### Careful

`island_policies` turns operator steering on too, because policies act through
it. Compare that arm against the `operators` arm, not only against the
baseline, or you will attribute the whole difference to the policy layer.

---

## T3 — Stock vs MAX benchmark (spec §16)

**Priority:** high. **Effort:** low to run, slow in wall-clock.
**Blocked by:** nothing.

### Why

The spec requires it before claiming success, and it is currently the largest
unfilled claim in `../benchmarks.md`.

### Where to start

The harness exists and both arms are model-matched:

```bash
./scripts/run-evolution.sh --profile stock --iterations 10
./scripts/run-evolution.sh --profile max   --iterations 10
```

Vary `random_seed` in the two configs across ≥5 seeds.

### Done when

`../benchmarks.md` has a table with ≥5 seeds per arm, reporting area under the
best-so-far curve **against requests**, not wall-clock.

### Careful

The baseline lacks truncation escalation, so it will inherit the 5-of-8 failure
mode. Report that as a provider-handling difference, separately from search
quality — conflating them would overstate the result.

---

## T4 — ~~Fix the urllib doctor~~ (DONE)

Completed. `control_plane/providers/doctor.py` now probes with `httpx`, keeps
urllib as a last-resort fallback, and classifies a Cloudflare 1010 as an
inconclusive transport block rather than a provider failure.

Two further corrections came out of it:

- `ModelProfile.requires_key` was added, because Zen serves Ox Alpha with no
  Authorization header and treating a missing key as disqualifying switched off
  a working primary route.
- The role chains led with Ox Alpha for tool-requiring roles once #44300 was
  resolved. **Superseded:** Ox Alpha was withdrawn by the provider and, on
  2026-08-27, removed from this repo entirely; NVIDIA NIM is now the primary
  and leads every chain. The honest distinction the original note drew still
  holds and is worth keeping: the *capability filter* self-corrects in both
  directions automatically, but the *chain order* is a stated preference and
  needs a deliberate edit every time.

## T5 — Sandbox executors (spec §9)

**Priority:** medium. **Effort:** large. **Blocked by:** nothing (Docker present).

### Why

The isolation *boundary* is built and tested — `control_plane/sandbox/opencode.py`
redirects HOME/XDG and refuses writes into operator-owned paths, with 9 tests.
What does not exist is the thing that runs a candidate inside it. Until it does,
candidates are evaluated by upstream's native evaluator in the engine's own
process tree, which is upstream's model and is documented as such in
`SECURITY.md`.

### Where to start

`control_plane/sandbox/`. Add a container backend: network disabled, CPU/RAM
limits, pids limit, wall timeout, output limit, isolated writable workdir, **no
secrets in the environment**. `sandbox_runs` and `agent_runs` tables and their
event families already exist and project correctly — nothing populates them yet.

### Done when

A candidate evaluates inside a container with no network and no keys, and the
Agent Sandbox page shows a real run instead of reporting the backend disabled.

### Careful

`OpenCodeIsolation.preflight()` fails closed by design. Keep it that way: if
isolation cannot be established the backend must stay disabled rather than
falling back to something weaker.

---

## T6 — Verification stages V1/V2 (spec §8)

**Priority:** medium. **Effort:** large. **Blocked by:** T5 for anything
untrusted.

Property, metamorphic, differential, randomised and hidden tests, then
symbolic/SMT checks and the independent critic. G0 and G1 are done
(`oe_max/evaluation/gates.py`); E0–E2 currently delegate to upstream's cascade.

Add suspicious-jump verification at the same time — an outlier improvement
should trigger fresh seeds and a larger hidden set before promotion.

---

## T7 — ~~Persist the rate-limiter window~~ (DONE)

Completed. `RateLimiter(state_path=…)` appends attempt starts and restores those
still inside the window on start; the NIM limiter uses
`.evolution/nim.window` by default (override with `OE_MAX_NIM_STATE`).

Deliberately conservative: aged-out entries are dropped, unparseable lines are
skipped, and the restore is capped at the window size so a corrupt or hostile
file cannot wedge the limiter shut. Persistence failures never block a request
the in-memory window already allowed — durability is best-effort, the bound is
not.

Still worth checking on the first real NIM run: watch the dashboard's
rolling-window gauge across a broker restart.

## T8 — Seed Forge and heterogeneous island policies (spec §7A, §7C)

**Priority:** low until T1–T3 land. **Effort:** medium.

### Seed Forge is no longer unmeasured (2026-08-29)

It could be measured without a model at all: the variants are deterministic AST
mutations, and `benchmarks/tasks/fn_min_seeded` scores a program identically
every time. Seven variants from the shipped seed, evaluated directly:

| variant | score | vs seed |
|---|---|---|
| the seed | 1.4061 | — |
| `scale_effort ×10.0` | **1.4742** | **+0.0681** |
| `scale_effort ×2.0` | 1.4513 | +0.0452 |
| `scale_literals ×2.0` | 1.3675 | −0.0386 |
| `scale_literals ×0.5` | 1.3391 | −0.0670 |
| `scale_effort ×0.5` | 0.9875 | −0.4185 |
| `scale_literals ×10.0` | 0.7439 | −0.6621 |

**Head start: yes.** Two of six beat the seed, the best by +0.068.

**Diversity: yes.** Six distinct scores spanning 0.74 to 1.47, so islands seeded
from this do not all start in the same basin — which is the stated rationale and
did not require any variant to be better.

The sharpest result is the comparison it invites. `scale_effort ×2.0` is exactly
`iterations=1000 → 2000`, and scores exactly 1.4513 — **the identical change, at
the identical score, that `qwen3:0.6b` took 30 iterations and three minutes of
model time to find.** The forge produces it in milliseconds, deterministically,
for free. A run seeded from the forge does not spend model calls rediscovering
the budget lever.

It is not a substitute for a model: the 27B's differential evolution scores
1.4987 and no forge variant approaches it. Head start, not answer.

### One defect found while measuring it

The forge round-trips through `ast.unparse`, which keeps docstrings and **drops
every comment**. On the shipped seed that loses all three:

    # EVOLVE-BLOCK-START
    # EVOLVE-BLOCK-END
    # This part remains fixed (not evolved)

Nothing breaks today — `parse_evolve_blocks` has no caller inside `openevolve/`
in the pinned version, so the markers are advisory rather than enforced. But the
third comment is an instruction addressed to the model, and a variant seeded into
the population is a program that no longer carries it. Whoever picks T8 up should
decide deliberately whether to preserve comments through the mutation rather than
inherit this by accident.

Generate multiple conceptually distinct starting families, and attach policies
(exploitation, structural, novelty, crossover, deep reasoning, wildcard) *above*
upstream's existing island mechanics.

Do not reimplement islands — upstream already has population, migration and
island assignment, and duplicating them would break the empty patch surface.

---

## T9 — The agent kernel has no timeout anywhere

**Priority:** low, but cheap and it is a hang rather than an error.
**Effort:** small. **Found:** 2026-08-29, sweeping for the timeout bug fixed in
`benchmarks/tasks/fn_min_seeded/evaluator.py`.

`control_plane/agent/kernel.py` submits every runnable task to a
`ThreadPoolExecutor` and then calls `future.result()` with **no timeout**, and
`_run_task` calls `runner(task)` directly with none either. Its own comment says
runners are model-supplied. So one runner that never returns hangs the whole
goal, with no event, no diagnostic, and — because the pool is entered with
`with` — a `shutdown(wait=True)` that will not return either.

### Why this is not already fixed

Choosing the number is a policy decision about that subsystem, and nothing here
has evidence about how long a legitimate task takes. Guessing one and calling it
a fix would trade a rare hang for routine spurious failures.

### What is worth knowing before picking it up

Python cannot kill a thread, so a timeout bounds the *wait* and leaks the
runner. That was acceptable for the evaluator and may not be here, where a
leaked runner holds tools open. `_run_with_timeout` in the seeded evaluator has
the working pattern and the two traps written down: a `with ThreadPoolExecutor`
joins the worker on exit and defeats the timeout entirely, and the executor's
non-daemon threads then block interpreter exit.

---

## Standing rules for whoever picks this up

- Do not edit `openevolve/`. Wrap at runtime instead.
- Do not put fixtures in `web/`. No data means "no data".
- Do not report a live test as passing unless it actually ran.
- Update `requirements.md` when a status changes, and add a decision to
  `../decisions.md` when you make a judgement call worth defending.
- Run `./test.sh` before pushing. The upstream suite runs first and is the
  regression gate.
