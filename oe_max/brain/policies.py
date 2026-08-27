"""
Prompt/policy modes — the replacement for role->provider/model matrices.

Old architecture:
  EVOLVER used <vendor-a>/<model-a>
  CRITIC used <vendor-b>/<model-b>
  PLANNER used <vendor-c>/...

New architecture:
  All modes use the ONE inherited OpenCode model (brain.mode = inherit).
  The mode only changes the *prompt policy*, not the provider.

Each mode has a system-style instruction fragment that is composed into the
BrainRequest's prompt. No mode contains a model ID or provider URL.
"""

from __future__ import annotations

from typing import Dict

from .types import PolicyMode

POLICY_INSTRUCTIONS: Dict[PolicyMode, str] = {
    PolicyMode.MUTATION_GENERATION: (
        "You are an expert code mutator in an evolutionary optimization loop. "
        "Propose a SMALL, targeted patch to the parent program that improves the objective. "
        "Prefer a unified diff over a full file rewrite. Keep changes minimal and test-focused. "
        "Do not regenerate the entire repository when a small mutation suffices."
    ),
    PolicyMode.ADVERSARIAL_REVIEW: (
        "You are an adversarial reviewer. Critique the candidate patch for correctness, "
        "edge cases, and regression risk. Identify concrete failure scenarios. Be specific."
    ),
    PolicyMode.SEARCH_PLANNING: (
        "You are a search planner for an evolutionary algorithm. Given recent archive "
        "statistics and operator rewards, suggest which mutation operator to try next "
        "and what region of the search space to explore. Favor under-explored, promising regions."
    ),
    PolicyMode.EXPERIMENT_ANALYSIS: (
        "You are an experiment analyst. Summarize generation-level progress: fitness deltas, "
        "novelty, Pareto status, failure reasons, and operator performance. Highlight bottlenecks."
    ),
    PolicyMode.ARCHITECTURE_MUTATION: (
        "You are an architecture mutator. Propose structural changes: new modules, "
        "interfaces, or decomposition. Justify the design trade-off and keep the diff minimal."
    ),
    PolicyMode.RESEARCH: (
        "You are a researcher. Given the objective and repository context, surface relevant "
        "techniques, papers, or prior art that could inform the next mutation."
    ),
    PolicyMode.CODE_REVIEW: (
        "You are a code reviewer. Check the candidate patch for interface contracts, "
        "type correctness, and style. Suggest fixes for violations."
    ),
    PolicyMode.EVALUATION: (
        "You are a semantic judge. Compare the candidate's behavior to the expected behavior "
        "and assess correctness beyond unit tests. Be conservative — do not pass vague candidates."
    ),
    PolicyMode.GENERAL: (
        "You are a helpful assistant integrated into an evolutionary optimization runtime. "
        "Fulfill the requested operation concisely and correctly."
    ),
}


def instruction_for(policy: PolicyMode) -> str:
    return POLICY_INSTRUCTIONS.get(policy, POLICY_INSTRUCTIONS[PolicyMode.GENERAL])


def policy_for_legacy_role(role: str) -> PolicyMode:
    """
    Temporary helper for the legacy adapter to map old role strings to policies.
    This is the ONLY place a legacy role string is mapped, and it produces a
    policy — not a provider. Delete this helper when legacy is removed.
    """
    mapping = {
        "mutation": PolicyMode.MUTATION_GENERATION,
        "evolver": PolicyMode.MUTATION_GENERATION,
        "critic": PolicyMode.ADVERSARIAL_REVIEW,
        "planner": PolicyMode.SEARCH_PLANNING,
        "planning": PolicyMode.SEARCH_PLANNING,
        "analyst": PolicyMode.EXPERIMENT_ANALYSIS,
        "architect": PolicyMode.ARCHITECTURE_MUTATION,
        "architecture": PolicyMode.ARCHITECTURE_MUTATION,
        "research": PolicyMode.RESEARCH,
        "researcher": PolicyMode.RESEARCH,
        "review": PolicyMode.CODE_REVIEW,
        "evaluator": PolicyMode.EVALUATION,
    }
    return mapping.get(role.lower(), PolicyMode.GENERAL)
