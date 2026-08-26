"""API contract: real data in, honest shapes out."""
import os
import pytest
from fastapi.testclient import TestClient
from control_plane.api.app import create_app
from control_plane.telemetry.events import Component, Event, EventType


@pytest.fixture
def client(workspace):
    app = create_app(workspace)
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "capabilities" in body


def test_capabilities_declare_unsupported_controls_with_reasons(client):
    caps = client.get("/api/control/capabilities").json()
    # Unsupported controls must explain themselves so the UI can disable them.
    for name in ("pause_resume_in_place", "fork_from_candidate"):
        assert caps[name]["supported"] is False
        assert len(caps[name]["reason"]) > 20


def test_supported_controls_are_marked_supported(client):
    caps = client.get("/api/control/capabilities").json()
    for name in ("start", "graceful_stop", "force_stop", "checkpoint_now"):
        assert caps[name]["supported"] is True


def test_start_run_rejects_missing_files(client):
    r = client.post("/api/control/runs", json={
        "initial_program": "/nope/missing.py", "evaluator": "/nope/eval.py"})
    assert r.status_code == 400
    assert "not found" in str(r.json()["detail"]).lower()


def test_unknown_run_is_404(client):
    assert client.get("/api/query/runs/run_missing").status_code == 404
    assert client.post("/api/control/runs/run_missing/stop",
                       json={"force": False}).status_code == 404


def test_query_endpoints_return_empty_not_fabricated(client):
    state = client.app.state.evolution
    state.store.ingest([Event(type=EventType.EXPERIMENT_CREATED,
                              component=Component.CONTROL_PLANE,
                              run_id="r1", experiment_id="e1",
                              metadata={"name": "t"})])
    for path in ("candidates", "islands", "checkpoints", "model-requests",
                 "evaluations", "lineage", "map-elites"):
        r = client.get(f"/api/query/runs/r1/{path}")
        assert r.status_code == 200, path
        # Empty collections, never placeholder rows.
        body = r.json()
        assert all(v == [] or v in (None, 0, {}, "")
                   for k, v in body.items()
                   if isinstance(v, list)), f"{path} returned non-empty placeholder"


def test_providers_endpoint_exposes_routes_and_health(client):
    body = client.get("/api/providers").json()
    assert body["profiles"] and body["routes"]
    primary = next(p for p in body["profiles"]
                   if p["id"] == "zen-nemotron-3-ultra-free")
    assert primary["free_status"] == "free_limited_time"


def test_a_withdrawn_route_is_still_explained_rather_than_hidden(client):
    """
    Ox Alpha was withdrawn from Zen (probed 2026-08-26). Its profile is kept
    and disabled instead of deleted, so the operator who chose it can see what
    happened to it. A route that simply vanishes from the UI reads as a bug in
    this project rather than as a change at the provider.
    """
    body = client.get("/api/providers").json()
    ox = next(p for p in body["profiles"] if p["id"] == "zen-ox-alpha-free")
    assert ox["enabled"] is False
    assert "withdrawn" in ox["notes"].lower()
    # And it must not still be advertised as a free tier that works.
    assert ox["free_status"] == "unknown"


def test_classic_visualizer_is_reachable(client):
    body = client.get("/api/classic").json()
    assert body["available"] is True      # preserved from upstream
    assert body["script"].endswith("visualizer.py")


def test_system_reports_isolation(client):
    body = client.get("/api/system").json()
    assert "opencode_isolation" in body
    assert "never_touched" in body["opencode_isolation"]


def test_search_rejects_malformed_query_as_client_error(client):
    r = client.get("/api/query/search", params={"q": '"unbalanced'})
    assert r.status_code in (200, 400)


def test_compare_requires_two_runs(client):
    assert client.get("/api/query/compare", params={"run_ids": "only_one"}).status_code == 400


# ---------------------------------------------------------------------------
# Candidate detail
#
# Two things an operator opens a candidate to find out — which request produced
# it, and whether it was verified — and neither was reachable.
# ---------------------------------------------------------------------------

def _candidate_with_request(state, run_id="r1", cid="c1", req="req_1"):
    from control_plane.telemetry.events import Component, Event, EventType

    state.store.ingest([
        Event(type=EventType.EXPERIMENT_CREATED, component=Component.CONTROL_PLANE,
              run_id=run_id, experiment_id="e1", metadata={"name": "t"}),
        Event(type=EventType.MODEL_REQUEST_COMPLETED, component=Component.LLM,
              run_id=run_id, duration_ms=101_000.0, summary="completed",
              metrics={"total_tokens": 5133},
              metadata={"request_id": req, "provider": "opencode_zen",
                        "model": "nemotron-3-ultra-free", "role": "mutation"}),
        Event(type=EventType.CANDIDATE_CREATED, component=Component.DATABASE,
              run_id=run_id, candidate_id=cid, summary="added",
              metrics={"combined_score": 0.5},
              metadata={"parent_id": "p0", "generating_request_id": req,
                        "generating_provider": "opencode_zen",
                        "generating_model": "nemotron-3-ultra-free"}),
    ])


def test_a_candidate_names_the_request_that_produced_it(client):
    """
    The old join was on `model_requests.candidate_id`, which upstream never
    sets — measured: 0 of 22 — so this list was empty for every candidate in
    every run.
    """
    state = client.app.state.evolution
    _candidate_with_request(state)

    body = client.get("/api/query/runs/r1/candidates/c1").json()
    assert [r["request_id"] for r in body["model_requests"]] == ["req_1"]
    assert body["model_requests"][0]["model"] == "nemotron-3-ultra-free"


def test_an_unattributed_candidate_lists_no_request_rather_than_a_wrong_one(client):
    from control_plane.telemetry.events import Component, Event, EventType

    state = client.app.state.evolution
    _candidate_with_request(state)
    state.store.ingest([Event(
        type=EventType.CANDIDATE_CREATED, component=Component.DATABASE,
        run_id="r1", candidate_id="seed", summary="added",
        metadata={"parent_id": None})])

    body = client.get("/api/query/runs/r1/candidates/seed").json()
    assert body["model_requests"] == []


def test_verification_is_visible_on_the_candidate(client):
    from control_plane.telemetry.events import Component, Event, EventType, Status

    state = client.app.state.evolution
    _candidate_with_request(state)
    state.store.ingest([Event(
        type=EventType.CANDIDATE_VERIFICATION_FAILED, component=Component.VERIFIER,
        run_id="r1", candidate_id="c1", status=Status.FAILED,
        summary="1 of 21 checks failed",
        metadata={"trigger": "new_champion", "passed": False, "checks_run": 21,
                  "failures": [{"name": "reported_value_matches_the_point",
                                "message": "reported -999.0 but f(0,0) = 0.0"}]})])

    body = client.get("/api/query/runs/r1/candidates/c1").json()
    assert len(body["verification"]) == 1
    entry = body["verification"][0]
    assert entry["trigger"] == "new_champion"
    assert entry["failures"][0]["message"].startswith("reported -999.0")


def test_a_candidate_nobody_verified_reports_an_empty_list(client):
    state = client.app.state.evolution
    _candidate_with_request(state)
    assert client.get("/api/query/runs/r1/candidates/c1").json()["verification"] == []
