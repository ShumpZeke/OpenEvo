# Continue prompt

Paste this as a `/goal` (or opening message) for the next AI session.

---

## Short version — for `/goal`

```
Finish OpenEvolve MAX in ShumpZeke/OpenEvo (branch main).

Read HANDOFF.md first, then NEXT_TASKS.md, then work the queue top-down.
Do not ask me what to do next — the queue is the answer. Work autonomously.

Rules that are load-bearing (CLAUDE.md has the full list):
- NEVER edit openevolve/ — it is byte-identical to upstream and must stay that
  way. Wrap public methods at runtime instead.
- No fake data in the UI, ever. No value means "no data", never a zero.
- Never claim a live test passed if it did not run. NVIDIA NIM and OpenRouter
  are unverified; keep saying so until a real key proves otherwise.
- Run ./test.sh before every push. 631 tests pass right now — keep them passing.
- Commit and push to main as you go. The container is ephemeral.

Update REQUIREMENTS_PROGRESS.md when a status changes, add to DECISIONS.md when
you make a judgement call worth defending, and put real measurements in
BENCHMARKS.md. If something you measure contradicts what the docs say, fix the
docs — the docs already record two of my own overclaims being corrected.

Start with T1 (fast-model routing). Keep going until the queue is done.
```

## Longer version — if the model needs more framing

```
You are continuing OpenEvolve MAX, a production fork of OpenEvolve with a
provider broker, telemetry control plane and browser Control Center. It is real,
running and tested — 631 tests, no known open defects — but roughly half the
original spec is deliberately unbuilt and documented as such.

Repository: ShumpZeke/OpenEvo, branch main.

1. Read HANDOFF.md completely. Section 3 is six traps that each cost hours to
   find; you will hit them otherwise.
2. Read NEXT_TASKS.md. It is a prioritised queue with rationale, a starting
   point, a done-when and a "how this goes wrong" note for each task.
3. Work top-down. T1 is the fast-model routing experiment; the measurements
   justifying that ordering are in BENCHMARKS.md.

The bar: build working, tested code — not scaffolding, not a plan, not a README.
Prove things by running them. When you cannot verify something (no credential,
no hardware), say precisely that and finish everything that does not depend on
it.

The single most important constraint: openevolve/ is byte-identical to upstream
411fb59c and that is what keeps upstream merges free. If you think you need to
edit it, you almost certainly need a runtime wrapper instead — see
control_plane/telemetry/instrument.py for the pattern.

Work autonomously. Do not ask which library to use or whether to create a
directory. Inspect, decide, implement, test, continue.
```

---

## What is already done, so it is not redone

Broker · NIM rate contract with a proven rolling-window invariant · live
provider discovery and smoke-testing · truncation detection with budget
escalation · retry, circuit breaking and failover · G0/G1 cascade gates · 15
mutation operator classes · discounted Thompson sampling · four research
archives · terminal dashboard · operator scripts for both platforms · 19-view
Control Center · full telemetry and storage.

## What is not, and is the actual work

Sandbox executors · V1/V2 verification stages · Seed Forge · heterogeneous
island policies · multi-offspring · the stock-vs-MAX benchmark with real seeds ·
counterexample DB · SymPy/Z3/Hypothesis integration.
