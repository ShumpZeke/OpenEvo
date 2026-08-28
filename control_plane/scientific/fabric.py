"""Capability metadata, routing, and safe optional-backend execution."""

from __future__ import annotations

import importlib.util
import shutil
from dataclasses import dataclass
from enum import Enum
from importlib.metadata import PackageNotFoundError, version

from .adapters import ADAPTER_TYPES, ScientificAdapter
from .ir import ScientificIR, ScientificToolResult, VerificationStatus


class CapabilityStatus(str, Enum):
    """Availability state measured locally at fabric construction time."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ScientificCapability:
    """Public metadata for one backend, safe to expose to an LLM director."""

    name: str
    version: str
    domain: str
    capabilities: tuple[str, ...]
    execution_mode: str
    deterministic: bool
    exact_or_approximate: str
    precision_support: str
    gpu_support: bool
    timeout_s: float
    memory_limit_mb: int | None
    dependencies: tuple[str, ...]
    license: str
    availability: CapabilityStatus
    cost: str
    confidence_semantics: str
    unavailable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _AdapterSpec:
    capability: ScientificCapability
    adapter: ScientificAdapter | None = None


class ScientificToolFabric:
    """Route structured problems to measured, optional scientific backends."""

    def __init__(self, specs: tuple[_AdapterSpec, ...] | None = None) -> None:
        self._specs = specs or _default_specs()

    def capabilities(self) -> tuple[ScientificCapability, ...]:
        """Return metadata without importing optional scientific packages."""
        return tuple(spec.capability for spec in self._specs)

    def route(self, problem: ScientificIR) -> tuple[ScientificCapability, ...]:
        """Select available capabilities using domain and structured requests."""
        requested = set(problem.requested_capabilities())
        matches = tuple(
            spec.capability
            for spec in self._specs
            if spec.capability.availability is CapabilityStatus.AVAILABLE
            and (not requested or requested.intersection(spec.capability.capabilities))
            and (
                spec.capability.domain == problem.domain or problem.domain == "general"
            )
        )
        if matches:
            return matches
        if requested:
            # The caller named the capability they need. Handing the problem to
            # whatever else happens to be installed produces a confusing answer
            # rather than an honest one -- a request to PROVE something was
            # being routed to numpy, which replied that it wanted a matrix.
            # Returning nothing lets `execute` say "no installed adapter can do
            # this", which is the fact the caller actually needs.
            return ()
        # No specific request: the caller is asking what could help at all, so
        # everything available is the right answer.
        return tuple(
            spec.capability
            for spec in self._specs
            if spec.capability.availability is CapabilityStatus.AVAILABLE
        )

    def execute(self, problem: ScientificIR) -> ScientificToolResult:
        """Execute the first routed adapter, preserving explicit failure evidence."""
        selected = self.route(problem)
        by_name = {spec.capability.name: spec for spec in self._specs}
        for capability in selected:
            adapter = by_name[capability.name].adapter
            if adapter is not None:
                return adapter.execute(problem)
        return ScientificToolResult(
            tool="scientific_fabric",
            status=VerificationStatus.INCONCLUSIVE,
            value="No installed adapter can execute this request.",
            warnings=("Install or configure one of the returned capabilities.",),
            provenance=tuple(capability.name for capability in selected),
        )


def _available_module(name: str) -> tuple[CapabilityStatus, str | None]:
    if importlib.util.find_spec(name) is not None:
        return CapabilityStatus.AVAILABLE, None
    return CapabilityStatus.UNAVAILABLE, f"Python module {name!r} is not installed"


def _module_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


def _default_specs() -> tuple[_AdapterSpec, ...]:
    specs: list[_AdapterSpec] = []
    for name, module, domain, capabilities, license_name in (
        ("sympy", "sympy", "algebra", ("symbolic_algebra",), "BSD-3-Clause"),
        (
            "numpy",
            "numpy",
            "numerical",
            ("numerical_simulation", "eigenvalues"),
            "BSD-3-Clause",
        ),
        (
            "scipy",
            "scipy",
            "numerical",
            ("optimization", "numerical_simulation"),
            "BSD-3-Clause",
        ),
        (
            "networkx",
            "networkx",
            "graph",
            ("graph_analysis", "shortest_path"),
            "BSD-3-Clause",
        ),
        ("jax", "jax", "numerical", ("autodiff", "gpu_acceleration"), "Apache-2.0"),
        ("cvxpy", "cvxpy", "optimization", ("convex_optimization",), "Apache-2.0"),
        (
            "ortools",
            "ortools",
            "optimization",
            ("constraint_optimization", "routing"),
            "Apache-2.0",
        ),
        ("qutip", "qutip", "quantum", ("quantum_dynamics",), "BSD-3-Clause"),
        ("astropy", "astropy", "astronomy", ("astronomy", "units"), "BSD-3-Clause"),
        ("openmm", "openmm", "molecular", ("molecular_dynamics",), "MIT"),
        ("pymatgen", "pymatgen", "materials", ("materials_science",), "MIT"),
    ):
        status, reason = _available_module(module)
        adapter_type = (
            ADAPTER_TYPES.get(name) if status is CapabilityStatus.AVAILABLE else None
        )
        specs.append(
            _AdapterSpec(
                ScientificCapability(
                    name=name,
                    version=(
                        _module_version(module)
                        if status is CapabilityStatus.AVAILABLE
                        else "unknown"
                    ),
                    domain=domain,
                    capabilities=capabilities,
                    execution_mode="native_python",
                    deterministic=True,
                    exact_or_approximate="exact_or_numeric",
                    precision_support="library-defined",
                    gpu_support=name == "jax",
                    timeout_s=30.0,
                    memory_limit_mb=None,
                    dependencies=(module,),
                    license=license_name,
                    availability=status,
                    cost="local",
                    confidence_semantics="backend-reported",
                    unavailable_reason=reason,
                ),
                adapter_type() if adapter_type is not None else None,
            )
        )
    for name, executable, domain, capabilities in (
        ("z3", "z3", "logic", ("constraint_solving",)),
        ("lean", "lean", "formal", ("formal_proof",)),
        ("sage", "sage", "algebra", ("symbolic_algebra", "formal_proof")),
    ):
        path = shutil.which(executable)
        specs.append(
            _AdapterSpec(
                ScientificCapability(
                    name=name,
                    version="installed" if path else "unknown",
                    domain=domain,
                    capabilities=capabilities,
                    execution_mode="native_binary",
                    deterministic=True,
                    exact_or_approximate="exact",
                    precision_support="backend-defined",
                    gpu_support=False,
                    timeout_s=120.0,
                    memory_limit_mb=None,
                    dependencies=(executable,),
                    license="unknown",
                    availability=(
                        CapabilityStatus.AVAILABLE
                        if path
                        else CapabilityStatus.UNAVAILABLE
                    ),
                    cost="local",
                    confidence_semantics="proof_or_solver_status",
                    unavailable_reason=(
                        None if path else f"Executable {executable!r} is not on PATH"
                    ),
                )
            )
        )
    return tuple(specs)
