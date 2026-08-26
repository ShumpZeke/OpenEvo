"""
Mutation operator taxonomy.

Upstream OpenEvolve issues one undifferentiated "improve this program" request.
Naming the *kind* of change buys two things:

  1. A sharper prompt. "Substitute a fundamentally different algorithm" and
     "tune a constant" produce very different edits; asking for one of them
     specifically beats asking for "an improvement" and hoping.
  2. Credit assignment. Once a mutation is labelled, the bandit can learn that
     (say) STRUCTURAL_REWRITE pays off early and PARAMETER_CHANGE pays off on a
     plateau — which is impossible when every request is the same request.

Operators are data. Adding one is a table entry, not a code change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class OperatorClass(str, Enum):
    LOCAL_OPTIMIZE = "LOCAL_OPTIMIZE"
    PARAMETER_CHANGE = "PARAMETER_CHANGE"
    STRUCTURAL_REWRITE = "STRUCTURAL_REWRITE"
    ALGORITHM_SUBSTITUTION = "ALGORITHM_SUBSTITUTION"
    REPRESENTATION_CHANGE = "REPRESENTATION_CHANGE"
    DECOMPOSE = "DECOMPOSE"
    SEARCH_SPACE_REDUCTION = "SEARCH_SPACE_REDUCTION"
    COMPLEXITY_REDUCTION = "COMPLEXITY_REDUCTION"
    NUMERICAL_STABILIZATION = "NUMERICAL_STABILIZATION"
    PROOF_GUIDED = "PROOF_GUIDED"
    COUNTEREXAMPLE_REPAIR = "COUNTEREXAMPLE_REPAIR"
    KNOWN_TECHNIQUE_INJECTION = "KNOWN_TECHNIQUE_INJECTION"
    RADICAL_RETHINK = "RADICAL_RETHINK"
    CROSS_LINEAGE_RECOMBINATION = "CROSS_LINEAGE_RECOMBINATION"
    ADVERSARIAL_REPAIR = "ADVERSARIAL_REPAIR"


@dataclass
class Operator:
    """One mutation class: what to ask for, and when it is applicable."""

    cls: OperatorClass
    instruction: str
    # Rough expected magnitude of change, used to bias exploration/exploitation
    # island policies rather than to score outcomes.
    disruption: float = 0.5
    # Some operators only make sense with extra context.
    needs_failure: bool = False          # a failing test / counterexample
    needs_second_parent: bool = False    # crossover
    enabled: bool = True

    def prompt_fragment(self) -> str:
        return f"MUTATION TYPE: {self.cls.value}\n{self.instruction}"


OPERATORS: Dict[OperatorClass, Operator] = {
    o.cls: o for o in [
        Operator(
            OperatorClass.LOCAL_OPTIMIZE,
            "Improve the existing approach without changing its structure. "
            "Tighten loops, remove redundant work, improve constant factors. "
            "Keep the same algorithm.",
            disruption=0.15,
        ),
        Operator(
            OperatorClass.PARAMETER_CHANGE,
            "Change only numeric parameters, thresholds, schedules or "
            "hyper-parameters. Do not alter control flow. Justify each value.",
            disruption=0.1,
        ),
        Operator(
            OperatorClass.STRUCTURAL_REWRITE,
            "Restructure the control flow or decomposition while keeping the "
            "underlying strategy. Reorganise loops, branches and helpers.",
            disruption=0.55,
        ),
        Operator(
            OperatorClass.ALGORITHM_SUBSTITUTION,
            "Replace the core algorithm with a fundamentally different one that "
            "solves the same problem. State which algorithm and why it suits "
            "this problem better.",
            disruption=0.85,
        ),
        Operator(
            OperatorClass.REPRESENTATION_CHANGE,
            "Change how the problem or solution is represented — different data "
            "structure, encoding, coordinate system or basis.",
            disruption=0.8,
        ),
        Operator(
            OperatorClass.DECOMPOSE,
            "Split the problem into sub-problems solved separately and combined. "
            "Introduce clear helper functions.",
            disruption=0.6,
        ),
        Operator(
            OperatorClass.SEARCH_SPACE_REDUCTION,
            "Prune or constrain the search space using a property of the problem, "
            "so less work reaches the same or a better answer.",
            disruption=0.5,
        ),
        Operator(
            OperatorClass.COMPLEXITY_REDUCTION,
            "Reduce asymptotic or practical complexity. State the before and "
            "after complexity explicitly.",
            disruption=0.6,
        ),
        Operator(
            OperatorClass.NUMERICAL_STABILIZATION,
            "Improve numerical robustness: avoid catastrophic cancellation, "
            "overflow, underflow and ill-conditioning. Preserve behaviour.",
            disruption=0.3,
        ),
        Operator(
            OperatorClass.PROOF_GUIDED,
            "Use a mathematical property, invariant or bound of the problem to "
            "justify a change. State the property you are relying on.",
            disruption=0.7,
        ),
        Operator(
            OperatorClass.COUNTEREXAMPLE_REPAIR,
            "Fix the specific failing case shown below without regressing cases "
            "that already pass.",
            disruption=0.35, needs_failure=True,
        ),
        Operator(
            OperatorClass.KNOWN_TECHNIQUE_INJECTION,
            "Introduce a well-established technique from the literature that is "
            "not yet present. Name the technique.",
            disruption=0.7,
        ),
        Operator(
            OperatorClass.RADICAL_RETHINK,
            "Ignore the current implementation's assumptions and propose a "
            "markedly different approach. Prefer a bold change over a safe one.",
            disruption=1.0,
        ),
        Operator(
            OperatorClass.CROSS_LINEAGE_RECOMBINATION,
            "Combine the strongest ideas from BOTH programs below into one that "
            "inherits the advantages of each.",
            disruption=0.75, needs_second_parent=True,
        ),
        Operator(
            OperatorClass.ADVERSARIAL_REPAIR,
            "A critic identified the weakness below. Address the underlying "
            "cause rather than special-casing the symptom.",
            disruption=0.45, needs_failure=True,
        ),
    ]
}


def applicable(
    *, has_failure: bool = False, has_second_parent: bool = False,
    exclude: Optional[List[OperatorClass]] = None,
) -> List[OperatorClass]:
    """
    Which operators can be used right now.

    Filtering on context matters: asking for COUNTEREXAMPLE_REPAIR with no
    counterexample produces a vague request, and the bandit would then learn
    that a perfectly good operator is useless.
    """
    excluded = set(exclude or [])
    out: List[OperatorClass] = []
    for cls, op in OPERATORS.items():
        if not op.enabled or cls in excluded:
            continue
        if op.needs_failure and not has_failure:
            continue
        if op.needs_second_parent and not has_second_parent:
            continue
        out.append(cls)
    return out


def build_prompt(
    op: OperatorClass, program: str, *,
    second_parent: Optional[str] = None,
    failure_context: Optional[str] = None,
    metrics: Optional[Dict[str, float]] = None,
) -> str:
    """Assemble the operator-specific user message."""
    o = OPERATORS[op]
    parts = [o.prompt_fragment(), "", "CURRENT PROGRAM:", "```python", program, "```"]

    if o.needs_second_parent and second_parent:
        parts += ["", "SECOND PROGRAM:", "```python", second_parent, "```"]
    if o.needs_failure and failure_context:
        parts += ["", "OBSERVED FAILURE / CRITIQUE:", failure_context]
    if metrics:
        parts += ["", "CURRENT METRICS:"] + [
            f"  {k}: {v}" for k, v in sorted(metrics.items())
        ]

    parts += [
        "",
        "Respond with one or more SEARCH/REPLACE blocks in exactly this format:",
        "",
        "<<<<<<< SEARCH",
        "(exact lines from the current program)",
        "=======",
        "(replacement lines)",
        ">>>>>>> REPLACE",
        "",
        "The SEARCH text must match the current program exactly, character for "
        "character. Emit the diff blocks before any closing commentary.",
    ]
    return "\n".join(parts)
