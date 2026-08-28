"""Typed intermediate representation for scientific problems and evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


class Objective(str, Enum):
    """Intent presented to the scientific capability router."""

    COMPUTE = "compute"
    SOLVE = "solve"
    PROVE = "prove"
    DISPROVE = "disprove"
    OPTIMIZE = "optimize"
    SIMULATE = "simulate"
    ESTIMATE = "estimate"
    CLASSIFY = "classify"
    DISCOVER = "discover"
    SEARCH = "search"
    VERIFY = "verify"
    DERIVE = "derive"
    FIT = "fit"
    INFER = "infer"


class VerificationStatus(str, Enum):
    """Evidence strength; statuses are intentionally not collapsed to success."""

    UNTESTED = "untested"
    HEURISTIC = "heuristic"
    NUMERICALLY_SUPPORTED = "numerically_supported"
    COMPUTATIONALLY_VERIFIED = "computationally_verified"
    SYMBOLICALLY_VERIFIED = "symbolically_verified"
    INTERVAL_CERTIFIED = "interval_certified"
    SAT_CONFIRMED = "sat_confirmed"
    UNSAT_CONFIRMED = "unsat_confirmed"
    FORMALLY_PROVED = "formally_proved"
    DISPROVED = "disproved"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True, slots=True)
class ScientificIR:
    """Machine-readable problem description passed between director and tools."""

    problem: str
    objective: Objective
    domain: str = "general"
    variables: tuple[str, ...] = ()
    variable_domains: Mapping[str, str] = field(default_factory=dict)
    constants: Mapping[str, str] = field(default_factory=dict)
    assumptions: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    equations: tuple[str, ...] = ()
    inequalities: tuple[str, ...] = ()
    units: Mapping[str, str] = field(default_factory=dict)
    dimensions: Mapping[str, str] = field(default_factory=dict)
    boundary_conditions: tuple[str, ...] = ()
    initial_conditions: tuple[str, ...] = ()
    definitions: tuple[str, ...] = ()
    known_results: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    invariants: tuple[str, ...] = ()
    symmetries: tuple[str, ...] = ()
    candidate_methods: tuple[str, ...] = ()
    solver_requests: tuple[str, ...] = ()
    transformations: tuple[str, ...] = ()
    solver_results: Mapping[str, str] = field(default_factory=dict)
    proof_obligations: tuple[str, ...] = ()
    counterexamples: tuple[str, ...] = ()
    numerical_precision: int | None = None
    error_bounds: Mapping[str, str] = field(default_factory=dict)
    residuals: Mapping[str, float] = field(default_factory=dict)
    verification_status: VerificationStatus = VerificationStatus.UNTESTED
    provenance: tuple[str, ...] = ()

    def requested_capabilities(self) -> tuple[str, ...]:
        """Return stable capability hints derived from structure, not keywords alone."""
        methods = set(self.candidate_methods) | set(self.solver_requests)
        if self.equations or self.inequalities:
            methods.add("symbolic_algebra")
        if self.constraints:
            methods.add("constraint_solving")
        if self.objective is Objective.OPTIMIZE:
            methods.add("optimization")
        if self.objective is Objective.SIMULATE:
            methods.add("numerical_simulation")
        return tuple(sorted(methods))


@dataclass(frozen=True, slots=True)
class ScientificToolResult:
    """Result plus provenance and independently meaningful verification status."""

    tool: str
    status: VerificationStatus
    value: str
    residual: float | None = None
    precision: int | None = None
    provenance: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
