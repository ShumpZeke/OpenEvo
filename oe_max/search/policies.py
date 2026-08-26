"""
Heterogeneous island policies.

Upstream's islands are structurally separate and behaviourally identical: every
island runs the same search, so migration exchanges programs between
populations that were exploring the same way. The diversity is in the
populations, not in how they got there.

Naming a *policy* per island changes what each one is for. An explorer island
asks for large, structural changes and mostly fails; an exploiter island tunes
what already works and mostly succeeds a little. Migration between them is then
worth something specific — the explorer supplies raw material the exploiter
would never have proposed, and the exploiter supplies a refined baseline the
explorer can wreck productively.

The mechanism is `Operator.disruption`, which every operator has declared since
the taxonomy was written and nothing read. A policy is a preference over that
axis, expressed as sampling weights rather than a hard filter: an explorer that
*never* tunes a parameter cannot finish anything, and one bad island policy
should cost some efficiency, not the whole island.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from .operators import OPERATORS, OperatorClass


@dataclass(frozen=True)
class IslandPolicy:
    """What one island is for, as a preference over operator disruption."""

    name: str
    # The disruption level this island aims at. Operators are weighted by how
    # close they sit to it.
    target_disruption: float
    # How sharply to prefer that target. Low = nearly uniform, high = narrow.
    sharpness: float
    description: str

    def weight_for(self, op: OperatorClass) -> float:
        """
        How strongly this island prefers one operator.

        A triangular falloff rather than a cutoff, and never zero: an explorer
        that can never tune a parameter cannot convert a structural idea into a
        working program, so every operator keeps a floor.
        """
        distance = abs(OPERATORS[op].disruption - self.target_disruption)
        return max(1.0 - self.sharpness * distance, MIN_WEIGHT)


# No operator is ever unreachable on any island. A policy is a bias, not a ban:
# a wrong bias should cost efficiency, not shut a search direction off entirely.
MIN_WEIGHT = 0.08

# Sharpness is measured, not guessed. Sampling 3,000 operators per policy over
# the 12 operators applicable with no failure and no second parent, mean
# disruption of what actually gets picked:
#
#   sharpness   exploit   refine   balanced   explore
#   1.0         0.464     0.524    0.566      0.648
#   2.0         0.314     0.458    0.566      0.712
#   3.0         0.294     0.438    0.566      0.740
#
# The unweighted taxonomy sits at 0.571. At 1.0 the islands barely separate; by
# 3.0 the MIN_WEIGHT floor is binding and the extra sharpness buys almost
# nothing. 2.0 puts explore and exploit either side of the baseline with a real
# gap, which is the whole point of naming them differently.
#
# `balanced` reproduces the unweighted mean to within 0.005, which is the check
# that the weighting itself introduces no bias of its own.
_SHARPNESS = 2.0

EXPLORE = IslandPolicy(
    "explore", target_disruption=0.85, sharpness=_SHARPNESS,
    description="large structural and algorithmic changes; expects to fail often")
EXPLOIT = IslandPolicy(
    "exploit", target_disruption=0.15, sharpness=_SHARPNESS,
    description="tune and tighten what already works; small reliable gains")
BALANCED = IslandPolicy(
    "balanced", target_disruption=0.5, sharpness=0.5,
    description="no strong preference; close to the unweighted taxonomy")
REFINE = IslandPolicy(
    "refine", target_disruption=0.35, sharpness=_SHARPNESS,
    description="restructure without replacing the underlying strategy")

POLICIES: Dict[str, IslandPolicy] = {
    p.name: p for p in (EXPLORE, EXPLOIT, BALANCED, REFINE)
}

# The order islands are assigned in. Exploit first so that a single-island run
# — and the first island of any run, which upstream seeds and samples from most
# — behaves conservatively rather than spending its whole budget on rewrites.
DEFAULT_ROTATION: Sequence[IslandPolicy] = (EXPLOIT, EXPLORE, BALANCED, REFINE)


def assign(num_islands: int,
           rotation: Optional[Sequence[IslandPolicy]] = None) -> List[IslandPolicy]:
    """
    Give every island a policy, round-robin over the rotation.

    Round-robin rather than random so two runs with the same island count are
    comparable — an experiment cannot attribute a difference to the policy
    layer if the layer itself differs run to run.
    """
    order = list(rotation or DEFAULT_ROTATION)
    if num_islands <= 0 or not order:
        return []
    return [order[i % len(order)] for i in range(num_islands)]


def policy_for(island: Optional[int], num_islands: int,
               rotation: Optional[Sequence[IslandPolicy]] = None) -> IslandPolicy:
    """The policy governing one island, or BALANCED when the island is unknown."""
    if island is None or num_islands <= 0:
        return BALANCED
    assigned = assign(num_islands, rotation)
    return assigned[island % len(assigned)] if assigned else BALANCED


def choose(policy: IslandPolicy, candidates: Sequence[OperatorClass],
           rng: random.Random) -> Optional[OperatorClass]:
    """
    Sample an operator under a policy's weights.

    Weighted sampling rather than argmax, deliberately: always picking the
    single best-fitting operator would collapse each island to one mutation
    class, which is a worse search than the uniform one it replaced.
    """
    options = list(candidates)
    if not options:
        return None
    weights = [policy.weight_for(op) for op in options]
    return rng.choices(options, weights=weights, k=1)[0]


def describe(num_islands: int) -> List[Dict[str, object]]:
    """Per-island policy, for telemetry and the Control Center."""
    return [
        {"island_id": i, "policy": p.name, "target_disruption": p.target_disruption,
         "description": p.description}
        for i, p in enumerate(assign(num_islands))
    ]
