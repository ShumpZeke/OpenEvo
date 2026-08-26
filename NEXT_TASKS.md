# Next Tasks

A prioritised, self-contained work queue. Each task states why it is worth
doing, where to start, how to know it worked, and what would make it a bad
idea. Ordered by expected value, not by convenience.

Read `HANDOFF.md` first — especially §3, the traps.

---

## T1 — Fast-model routing experiment

**Priority:** highest. **Effort:** small — the machinery is built.
**Blocked by:** nothing. **Status:** run it and read the answer.

### Why

Measured on real runs, and the numbers move:

| route | success | p50 latency | when |
|---|---|---|---|
| `x-preview-f-free` (Ox Alpha) | 40% | 220 s | first measurement |
| `x-preview-f-free` (Ox Alpha) | 26% | 284 s | later, same week |
| `nemotron-3-ultra-free` | 100% | 112 s | first measurement |

If a cheaper route produces comparable mutation quality, throughput roughly
doubles. The objective divides by wall-clock and API requests, so this attacks
the denominator directly. That Ox Alpha's own numbers moved by that much
between measurements is itself the argument for re-running rather than
trusting a table.

### What is already built

The three things this used to need are done:

1. **Attribution.** Every candidate carries the model request that generated
   it, across the worker→main process boundary
   (`control_plane/telemetry/instrument.py`, `ATTRIBUTION_KEY`). Verified live:
   12 of 15 candidates attributed, the other 3 being the seed program and two
   migrant copies, which are unattributable by design.
2. **Quality per route.** `oe_max/route_quality.py` defines the measures;
   `control_plane/analysis/route_quality.py` builds them from stored telemetry;
   `GET /api/query/runs/{id}/route-quality?pool=…` serves them.
3. **The experiment itself.** `scripts/route-experiment.sh` runs one arm per
   route off a single base config, pools the runs per route, and prints a
   verdict that refuses to name a winner on thin evidence.

### How to run it

```bash
./scripts/start-broker.sh                        # terminal 1
./run.sh                                         # terminal 2
./scripts/route-experiment.sh \
    --routes x-preview-f-free,nemotron-3-ultra-free \
    --iterations 12 --repeats 3
```

Three repeats per arm is the point: `MIN_ATTEMPTS_FOR_COMPARISON` is 20, and a
12-iteration run does not get there alone. `--min-attempts` can lower the bar,
but lowering it is a claim you then have to defend.

### What one run already showed

A three-arm run is recorded in BENCHMARKS.md. The headline is that the two
efficiency views **disagree**: Ox Alpha leads on improvement per *request*
(0.331 vs 0.229) and loses badly on improvement per *second* (7.9e-04 vs
2.7e-03), because it is ~5x slower. That is the trade-off to put in front of
the operator, not a switch to make.

It is not enough evidence: the arms were 4, 8 and 12 attempts against a
minimum of 20, and two of them were cut off by a timeout and a container
restart rather than by the design.

A second attempt pooled `nemotron-3-ultra-free` to **24 attempts** — the first
route ever to clear the minimum — and was called off part way through the Ox
Alpha arms, which had produced 2 requests in 32 minutes at 11% success. See
BENCHMARKS.

**So the blocker on T1 is not the harness, it is the route.** Before spending
hours on this again, check Ox Alpha's live success rate
(`./scripts/verify-providers.sh`, or the ROUTE QUALITY section of
`./scripts/dashboard.sh`). Below ~25% it will now be demoted out of the chain
automatically, and an arm pinned to it is a waiting game with no result at the
end.

### Done when

You can answer "does Ox Alpha produce better mutations than
`nemotron-3-ultra-free`, and by how much per second?" with numbers from ≥3 runs
per arm, and the verdict is not "insufficient evidence".

### Careful

The operator explicitly chose Ox Alpha as primary. **Do not change the default
route on latency alone.** Bring evidence about quality, then propose it. If Ox
Alpha is better per *request* but worse per *minute*, that is a genuine
trade-off for the operator to decide, not for you to silently resolve.

Read the three views before concluding anything. `improvement_per_request` is
what matters under a rate contract, `improvement_per_second` when wall-clock is
the constraint, `improvement_per_1k_tokens` when someone is paying. The same
two routes can rank differently under each, and that is a result rather than a
contradiction.

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
HANDOFF §3.3 — that is one coupled setting, not three).

---

## T2b — Run the ablations against a real provider

**Priority:** high. **Effort:** small to run, hours to wait.
**Blocked by:** nothing. **Status:** harness built and smoke-tested; no result.

### Why

Four features are marked PARTIAL in `REQUIREMENTS_PROGRESS.md` for the same
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
unfilled claim in `BENCHMARKS.md`.

### Where to start

The harness exists and both arms are model-matched:

```bash
./scripts/run-evolution.sh --profile stock --iterations 10
./scripts/run-evolution.sh --profile max   --iterations 10
```

Vary `random_seed` in the two configs across ≥5 seeds.

### Done when

`BENCHMARKS.md` has a table with ≥5 seeds per arm, reporting area under the
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
- The role chains now lead with Ox Alpha for tool-requiring roles too, since
  #44300 is resolved and tools are verified. Note the honest distinction: the
  *capability filter* self-corrects in both directions automatically, but the
  *chain order* is a stated preference and needed a deliberate edit.

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

Generate multiple conceptually distinct starting families, and attach policies
(exploitation, structural, novelty, crossover, deep reasoning, wildcard) *above*
upstream's existing island mechanics.

Do not reimplement islands — upstream already has population, migration and
island assignment, and duplicating them would break the empty patch surface.

---

## Standing rules for whoever picks this up

- Do not edit `openevolve/`. Wrap at runtime instead.
- Do not put fixtures in `web/`. No data means "no data".
- Do not report a live test as passing unless it actually ran.
- Update `REQUIREMENTS_PROGRESS.md` when a status changes, and add a decision to
  `DECISIONS.md` when you make a judgement call worth defending.
- Run `./test.sh` before pushing. The upstream suite runs first and is the
  regression gate.
