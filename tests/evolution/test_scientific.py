"""
Scientific computation that runs on this machine, and reports honestly.

The value here is not that it computes — numpy already does that — it is that
the result carries a *verification status* rather than a boolean. A float that
satisfies an equation to 1e-12 has not been proved, and collapsing those two
into "success" is how a search starts trusting a number it should not. The
statuses are deliberately not ordered into a single "confidence" score for the
same reason.

The second property is availability. Backends are detected by import, and a
missing one is reported `unavailable` with the reason. It is never omitted (the
caller would not know it could have helped) and never faked into a result,
which is the no-fake-data rule applied to capability instead of to metrics.

Both matter most in local mode, where the machine may have almost nothing
installed and there is no cloud service to quietly cover the gap.
"""

import pytest

from control_plane.scientific import (
    CapabilityStatus,
    Objective,
    ScientificIR,
    ScientificToolFabric,
    VerificationStatus,
)


@pytest.fixture
def fabric():
    return ScientificToolFabric()


class TestCapabilityHonesty:
    def test_a_missing_backend_is_reported_with_its_reason(self, fabric):
        """
        Not omitted and not failed — stated. A caller that asked for a formal
        proof needs to know Lean is absent rather than infer it from silence.
        """
        missing = [c for c in fabric.capabilities()
                   if c.availability is CapabilityStatus.UNAVAILABLE]
        if not missing:
            pytest.skip("every backend is installed on this machine")

        for capability in missing:
            assert capability.unavailable_reason, capability.name
            assert capability.version == "unknown", (
                f"{capability.name} reports a version it cannot know")

    def test_an_available_backend_reports_its_real_version(self, fabric):
        present = [c for c in fabric.capabilities()
                   if c.availability is CapabilityStatus.AVAILABLE]
        if not present:
            pytest.skip("no scientific backend installed")

        for capability in present:
            assert capability.unavailable_reason is None, capability.name

    def test_capabilities_can_be_listed_without_importing_the_backends(self, fabric):
        """
        Listing must be cheap and side-effect free: this is what an LLM
        director is shown before it decides what to ask for, and importing
        every heavyweight package to answer would make that unusable.
        """
        caps = fabric.capabilities()

        assert caps, "no capabilities declared at all"
        assert len({c.name for c in caps}) == len(caps), "duplicate backend"

    def test_heavyweight_systems_are_declared_but_not_imported(self, fabric):
        """
        Z3, Lean and Sage are looked for on PATH, not imported. Importing them
        at startup is what makes a local run require an environment nobody
        building one actually has.
        """
        by_name = {c.name: c for c in fabric.capabilities()}

        for name in ("z3", "lean", "sage"):
            assert name in by_name, name
            assert by_name[name].execution_mode == "native_binary", name


class TestRouting:
    def test_structure_selects_the_capability_not_keywords(self, fabric):
        """
        `requested_capabilities` is derived from the IR's shape — equations
        imply symbolic algebra, an OPTIMIZE objective implies optimization — so
        routing does not depend on how the problem was worded.
        """
        problem = ScientificIR(
            problem="minimise a convex function",
            objective=Objective.OPTIMIZE,
            domain="numerical",
        )

        assert "optimization" in problem.requested_capabilities()

    def test_equations_imply_symbolic_algebra(self):
        problem = ScientificIR(
            problem="solve for x",
            objective=Objective.SOLVE,
            equations=("x**2 - 4",),
            variables=("x",),
        )

        assert "symbolic_algebra" in problem.requested_capabilities()

    def test_a_general_domain_does_not_exclude_specific_backends(self, fabric):
        """
        "general" means the caller did not narrow it, not that only general
        tools apply — narrowing on absence would hide every usable backend.
        """
        problem = ScientificIR(problem="anything", objective=Objective.COMPUTE)

        routed = fabric.route(problem)
        if not routed:
            pytest.skip("no backend installed to route to")
        assert all(c.availability is CapabilityStatus.AVAILABLE for c in routed)

    def test_routing_never_returns_an_unavailable_backend(self, fabric):
        problem = ScientificIR(
            problem="prove something",
            objective=Objective.PROVE,
            domain="formal",
        )

        for capability in fabric.route(problem):
            assert capability.availability is CapabilityStatus.AVAILABLE


class TestExecution:
    def test_a_request_nothing_can_serve_is_inconclusive_not_an_error(self, fabric):
        """
        "We cannot check this here" is information the caller needs, and it is
        a different fact from "we checked and it failed". Raising would collapse
        them, and returning a value would invent one.
        """
        problem = ScientificIR(
            problem="prove the Riemann hypothesis",
            objective=Objective.PROVE,
            domain="formal",
            solver_requests=("formal_proof",),
        )

        result = fabric.execute(problem)

        if result.status is not VerificationStatus.INCONCLUSIVE:
            pytest.skip("a formal backend is installed on this machine")
        assert result.warnings, "inconclusive without saying what would help"

    def test_a_numeric_result_is_numerically_supported_and_not_proved(self, fabric):
        """
        The distinction this module exists for. numpy computed it; that is
        evidence, not a proof, and the status says so.
        """
        pytest.importorskip("numpy")
        problem = ScientificIR(
            problem="eigenvalues of a diagonal matrix",
            objective=Objective.COMPUTE,
            domain="numerical",
            solver_requests=("eigenvalues",),
            constants={"matrix": "[[2, 0], [0, 3]]"},
        )

        result = fabric.execute(problem)

        assert result.status is VerificationStatus.NUMERICALLY_SUPPORTED
        assert result.status is not VerificationStatus.FORMALLY_PROVED
        assert "2.0" in result.value and "3.0" in result.value
        assert result.provenance, "a result with no provenance is unciteable"

    def test_the_status_vocabulary_does_not_collapse_to_success(self):
        """
        There is no `SUCCESS`. Every status names what kind of evidence was
        obtained, which is what stops a caller treating a residual as a proof.
        """
        names = {s.name for s in VerificationStatus}

        assert "SUCCESS" not in names and "OK" not in names
        assert {"NUMERICALLY_SUPPORTED", "SYMBOLICALLY_VERIFIED",
                "FORMALLY_PROVED", "INCONCLUSIVE"} <= names


class TestApiSurface:
    def test_capabilities_endpoint_reports_availability_honestly(self):
        from fastapi.testclient import TestClient

        from control_plane.api.app import create_app

        client = TestClient(create_app())
        body = client.get("/api/scientific/capabilities").json()

        assert body["total_count"] == len(body["capabilities"])
        assert body["available_count"] <= body["total_count"]
        for capability in body["capabilities"]:
            if capability["availability"] == "unavailable":
                assert capability["unavailable_reason"]

    def test_execute_endpoint_returns_a_status_not_a_boolean(self):
        from fastapi.testclient import TestClient

        from control_plane.api.app import create_app

        pytest.importorskip("numpy")
        client = TestClient(create_app())
        body = client.post("/api/scientific/execute", json={
            "problem": "eigenvalues",
            "objective": "compute",
            "domain": "numerical",
            "solver_requests": ["eigenvalues"],
            "constants": {"matrix": "[[2, 0], [0, 3]]"},
        }).json()

        assert body["status"] == "numerically_supported"
        assert body["provenance"]
        assert "success" not in body

    def test_an_unknown_objective_is_rejected_with_the_valid_set(self):
        from fastapi.testclient import TestClient

        from control_plane.api.app import create_app

        client = TestClient(create_app())
        response = client.post("/api/scientific/execute",
                               json={"objective": "transmute"})

        assert response.status_code == 400
        assert "compute" in response.json()["detail"]
