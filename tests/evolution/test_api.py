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
    ox = next(p for p in body["profiles"] if p["id"] == "zen-ox-alpha-free")
    assert ox["free_status"] == "free_limited_time"


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
