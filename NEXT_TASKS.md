# Next Tasks

A prioritised, self-contained work queue. Each task states why it is worth
doing, where to start, how to know it worked, and what would make it a bad
idea. Ordered by expected value, not by convenience.

Read `HANDOFF.md` first — especially §3, the traps.

---

## T1 — Fast-model routing experiment

**Priority:** highest. **Effort:** medium. **Blocked by:** nothing.

### Why

Measured on a real run:

| route | success | avg latency |
|---|---|---|
| `x-preview-f-free` (Ox Alpha) | 40% | 220 s |
| `nemotron-3-ultra-free` | 100% | 112 s |

If a cheaper route produces comparable mutation quality, throughput roughly
doubles. The spec's objective divides by wall-clock and API requests, so this
attacks the denominator directly.

### Where to start

`oe_max/router.py` already records per-route statistics
(`Router.stats_by_route`). What is missing is *quality* per route, not just
reliability.

1. Label each generation with the route that served it (the provenance is
   already on every response as `oe_max.provider` / `oe_max.model`).
2. Record whether the resulting candidate passed G0/G1 and its fitness delta.
3. Aggregate: mutation-validity rate and mean fitness delta **per route**.

### Done when

You can answer "does Ox Alpha produce better mutations than
`nemotron-3-ultra-free`, and by how much per second?" with numbers from ≥3 runs.

### Careful

The operator explicitly chose Ox Alpha as primary. **Do not change the default
route on latency alone.** Bring evidence about quality, then propose it. If Ox
Alpha is better per *request* but worse per *minute*, that is a genuine
trade-off for the operator to decide, not for you to silently resolve.

---

## T2 — Multi-offspring per request (spec §7F)

**Priority:** high. **Effort:** medium. **Blocked by:** nothing.

### Why

At ~220 s and ~8,000 tokens per request, extracting 2–3 diverse candidates from
one request is close to a linear throughput win. The latency measurement is what
makes this valuable here specifically.

### Where to start

`oe_max/search/operators.py::build_prompt`. Ask for N clearly separated
alternatives, parse them apart, and push each through the existing
`evaluation/gates.py` G0 → G1 chain. The dedup index will tell you immediately
whether the "diverse" alternatives are actually diverse — that is the thing to
measure.

### Done when

A benchmark shows candidates-per-request and *useful* candidates-per-request
against the single-offspring baseline over ≥3 seeds.

### Careful

The spec says do not enable globally until benchmarked. The failure mode is
three near-identical candidates that all collapse to one AST hash — throughput
that is not real. `DedupIndex.stats()` measures exactly that; watch it.

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

## T7 — Persist the rate-limiter window

**Priority:** low. **Effort:** small. **Blocked by:** nothing.

The NIM limiter's rolling window is in-process, so a broker restart forgets it
and a burst immediately afterwards could exceed the 48 RPM contract. Persist
attempt-start timestamps (a small append-only file is enough) and reload on
start.

Only matters once a real `NVIDIA_API_KEY` is in use — but it is a correctness
gap in the one invariant the spec calls absolute, so it should not be forgotten.

---

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
