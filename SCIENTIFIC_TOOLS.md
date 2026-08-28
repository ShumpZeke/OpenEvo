# Scientific tools

Structured scientific computation, routed to whatever backends are installed on
this machine. Everything here runs locally and needs no credential, which is what
makes it usable in [fully local mode](LOCAL_MODE.md).

```bash
uv pip install -e ".[scientific]"                        # sympy, scipy, networkx
curl http://127.0.0.1:8000/api/scientific/capabilities
```

## The two properties that matter

**Results carry a verification status, not a boolean.** There is no `SUCCESS` in
the vocabulary and that is deliberate:

| status | means |
|---|---|
| `symbolically_verified` | a symbolic system established it |
| `computationally_verified` | an exact computation established it |
| `numerically_supported` | floating point agrees, which is evidence, not proof |
| `interval_certified` | bounded with rigorous arithmetic |
| `sat_confirmed` / `unsat_confirmed` | a solver decided it |
| `formally_proved` | a proof assistant checked it |
| `heuristic` | a plausible answer with no verification |
| `inconclusive` | nothing installed could decide it |
| `disproved` | a counterexample exists |
| `untested` | nothing has been run |

A float satisfying an equation to 1e-12 has not been proved. Collapsing that into
"success" is how a search starts trusting a number it should not — and this
project's whole output is a comparison between candidates, so a status that
overstates its evidence corrupts the comparison rather than one answer.

The statuses are also deliberately **not** ordered into a single confidence
score. `numerically_supported` and `unsat_confirmed` are not points on one scale.

**Availability is measured, not declared.** Backends are detected by import (or
by `PATH` for the binaries). A missing one is reported `unavailable` *with the
reason*:

```json
{"name": "sympy", "availability": "unavailable",
 "unavailable_reason": "Python module 'sympy' is not installed"}
```

It is never omitted — a caller would not know it could have helped — and never
faked into a result. This is the no-fake-data rule applied to capability instead
of to metrics.

## Executable adapters

| Backend | Domain | Request | Evidence |
|---|---|---|---|
| SymPy | algebra | equations and variables | `symbolically_verified` |
| NumPy | numerical | `eigenvalues` plus a JSON matrix | `numerically_supported` |
| SciPy | numerical | `minimize_quadratic` plus JSON vectors | `numerically_supported` |
| NetworkX | graph | `shortest_path` plus JSON edges/source/target | `computationally_verified` |

Declared and detected, but without an executable adapter yet: JAX, CVXPY,
OR-Tools, QuTiP, Astropy, OpenMM, PyMatGen (Python), and Z3, Lean, Sage (looked
for on `PATH`). They are listed so a router can say "this would have been the
right tool", and calling one returns `inconclusive` rather than a fabricated
answer.

Heavyweight systems are deliberately **not imported at startup**. Requiring them
would make a local run need an environment nobody building one actually has.

## Routing

Problems are described as a `ScientificIR` — a typed record of the objective,
domain, variables, equations, constraints and solver requests — and capabilities
are derived from that *structure* rather than from keywords in the prose:

- equations or inequalities present → `symbolic_algebra`
- constraints present → `constraint_solving`
- objective `OPTIMIZE` → `optimization`
- objective `SIMULATE` → `numerical_simulation`

So routing does not change because the problem was worded differently.

**A named request that nothing provides returns `inconclusive`.** It is not
handed to whatever else is installed. That was the original behaviour and it
produced nonsense: a request to *prove* something reached NumPy, which replied
that it wanted a matrix. When no capability is named at all, everything
available is returned — the caller is asking what could help, and that is a
different question.

## API

```
GET  /api/scientific/capabilities     what this machine can do, and why not
POST /api/scientific/execute          run one structured problem
```

```bash
curl -X POST http://127.0.0.1:8000/api/scientific/execute \
  -H 'Content-Type: application/json' -d '{
    "problem": "eigenvalues of a diagonal matrix",
    "objective": "compute",
    "domain": "numerical",
    "solver_requests": ["eigenvalues"],
    "constants": {"matrix": "[[2, 0], [0, 3]]"} }'
```

```json
{"tool": "numpy", "status": "numerically_supported", "value": "[2.0, 3.0]",
 "provenance": ["numpy.linalg.eigvals", "floating-point computation"]}
```

An unknown objective is rejected with the valid set rather than silently
defaulting, because a silently defaulted objective routes to the wrong backend
and returns a confident answer to a question nobody asked.

## Tests

`tests/evolution/test_scientific.py`. They assert the honesty properties rather
than the arithmetic: that a missing backend reports its reason, that a numeric
result is not labelled proved, that the vocabulary contains no `SUCCESS`, and
that an unservable request is `inconclusive` rather than an error or a guess.
