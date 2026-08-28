from __future__ import annotations

import json
from importlib import import_module
from typing import Mapping, Protocol

from .ir import ScientificIR, ScientificToolResult, VerificationStatus


class ScientificAdapter(Protocol):
    def execute(self, problem: ScientificIR) -> ScientificToolResult: ...


class _SympyAdapter:
    def execute(self, problem: ScientificIR) -> ScientificToolResult:
        sympy = import_module("sympy")

        if not problem.equations or not problem.variables:
            return ScientificToolResult(
                tool="sympy",
                status=VerificationStatus.INCONCLUSIVE,
                value="SymPy solve requires equations and variables.",
            )
        try:
            symbols = sympy.symbols(problem.variables)
            equations = [
                sympy.sympify(equation.replace("=", "-"))
                for equation in problem.equations
            ]
            values = sympy.solve(equations, symbols, dict=True)
        except (SyntaxError, TypeError, ValueError) as exc:
            return ScientificToolResult(
                tool="sympy",
                status=VerificationStatus.INCONCLUSIVE,
                value=f"SymPy could not parse the request: {type(exc).__name__}",
            )
        return ScientificToolResult(
            tool="sympy",
            status=VerificationStatus.SYMBOLICALLY_VERIFIED,
            value=str(values),
            provenance=("sympy.solve", "exact symbolic result"),
        )


class _NumpyAdapter:
    def execute(self, problem: ScientificIR) -> ScientificToolResult:
        import numpy

        matrix_text = problem.constants.get("matrix")
        if "eigenvalues" not in problem.solver_requests or matrix_text is None:
            return ScientificToolResult(
                tool="numpy",
                status=VerificationStatus.INCONCLUSIVE,
                value="NumPy adapter currently requires a matrix eigenvalue request.",
            )
        try:
            matrix = numpy.asarray(json.loads(matrix_text), dtype=float)
            values = numpy.linalg.eigvals(matrix)
        except (
            json.JSONDecodeError,
            TypeError,
            ValueError,
            numpy.linalg.LinAlgError,
        ) as exc:
            return ScientificToolResult(
                tool="numpy",
                status=VerificationStatus.INCONCLUSIVE,
                value=f"NumPy could not evaluate the request: {type(exc).__name__}",
            )
        normalized = numpy.real_if_close(values)
        serializable = (
            [
                {"real": float(value.real), "imag": float(value.imag)}
                for value in normalized
            ]
            if numpy.iscomplexobj(normalized)
            else normalized.tolist()
        )
        return ScientificToolResult(
            tool="numpy",
            status=VerificationStatus.NUMERICALLY_SUPPORTED,
            value=json.dumps(serializable),
            provenance=("numpy.linalg.eigvals", "floating-point computation"),
        )


class _NetworkxAdapter:
    def execute(self, problem: ScientificIR) -> ScientificToolResult:
        networkx = import_module("networkx")

        edges_text = problem.constants.get("edges")
        source = problem.constants.get("source")
        target = problem.constants.get("target")
        if (
            "shortest_path" not in problem.solver_requests
            or edges_text is None
            or source is None
            or target is None
        ):
            return ScientificToolResult(
                tool="networkx",
                status=VerificationStatus.INCONCLUSIVE,
                value="NetworkX adapter requires edges, source, target, and shortest_path.",
            )
        try:
            graph = networkx.Graph()
            graph.add_edges_from(json.loads(edges_text))
            path = networkx.shortest_path(graph, source=source, target=target)
        except (
            json.JSONDecodeError,
            TypeError,
            ValueError,
            networkx.NetworkXError,
        ) as exc:
            return ScientificToolResult(
                tool="networkx",
                status=VerificationStatus.INCONCLUSIVE,
                value=f"NetworkX could not evaluate the request: {type(exc).__name__}",
            )
        return ScientificToolResult(
            tool="networkx",
            status=VerificationStatus.COMPUTATIONALLY_VERIFIED,
            value=json.dumps(path),
            provenance=("networkx.shortest_path", "deterministic graph result"),
        )


class _ScipyAdapter:
    def execute(self, problem: ScientificIR) -> ScientificToolResult:
        import numpy

        minimize = import_module("scipy.optimize").minimize

        initial_text = problem.constants.get("initial")
        target_text = problem.constants.get("target")
        if (
            "minimize_quadratic" not in problem.solver_requests
            or initial_text is None
            or target_text is None
        ):
            return ScientificToolResult(
                tool="scipy",
                status=VerificationStatus.INCONCLUSIVE,
                value="SciPy adapter requires initial, target, and minimize_quadratic.",
            )
        try:
            initial = numpy.asarray(json.loads(initial_text), dtype=float)
            target = numpy.asarray(json.loads(target_text), dtype=float)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            return ScientificToolResult(
                tool="scipy",
                status=VerificationStatus.INCONCLUSIVE,
                value=f"SciPy could not evaluate the request: {type(exc).__name__}",
            )
        if initial.shape != target.shape or initial.ndim != 1:
            return ScientificToolResult(
                tool="scipy",
                status=VerificationStatus.INCONCLUSIVE,
                value="SciPy could not evaluate the request: ValueError",
            )
        result = minimize(
            lambda values: float(numpy.sum((values - target) ** 2)), initial
        )
        status = (
            VerificationStatus.NUMERICALLY_SUPPORTED
            if result.success
            else VerificationStatus.INCONCLUSIVE
        )
        return ScientificToolResult(
            tool="scipy",
            status=status,
            value=json.dumps(result.x.tolist()),
            residual=float(result.fun),
            provenance=(
                "scipy.optimize.minimize",
                "quadratic objective",
                result.message,
            ),
        )


ADAPTER_TYPES: Mapping[str, type[ScientificAdapter]] = {
    "sympy": _SympyAdapter,
    "numpy": _NumpyAdapter,
    "scipy": _ScipyAdapter,
    "networkx": _NetworkxAdapter,
}
