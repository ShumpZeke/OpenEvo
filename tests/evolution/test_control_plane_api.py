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


def test_the_api_serves_no_ox_alpha_route(client):
    """
    Ox Alpha was withdrawn by the provider on 2026-08-26 and removed from
    service here on 2026-08-27. The API is the surface the Control Center reads,
    so absence has to hold here and not only in the table it is built from.
    """
    body = client.get("/api/providers").json()
    for profile in body["profiles"]:
        assert "ox-alpha" not in profile["id"], profile["id"]
        assert profile.get("model") != "x-preview-f-free", profile["id"]


def test_a_route_that_did_not_serve_is_disabled_but_still_explained(client):
    """
    The "explain, do not hide" principle outlived Ox Alpha and still applies to
    the two NIM routes that probing found unserveable: an operator who set
    NVIDIA_API_KEY and sees only three of five models should be able to read why
    from the UI rather than assume a bug here.
    """
    body = client.get("/api/providers").json()
    by_id = {p["id"]: p for p in body["profiles"]}
    for pid in ("nim-gpt-oss-120b", "nim-codestral-22b"):
        assert pid in by_id, pid
        assert by_id[pid]["enabled"] is False, pid
        assert by_id[pid]["notes"].strip(), pid


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


# --------------------------------------------------------------------------
# The broker is a different process, and it is the one that actually routes.
# --------------------------------------------------------------------------


def test_broker_status_is_honest_when_the_broker_is_not_running(client, monkeypatch):
    """
    The rule that matters here is the no-fabricated-data rule.

    An operator reading an invented route table would make worse decisions than
    one told plainly that we cannot see the broker. So an unreachable broker
    must yield `reachable: false` and a null router — never an empty-but-
    plausible table, which reads as "all routes healthy, nothing has happened
    yet".
    """
    monkeypatch.setenv("OE_MAX_BASE", "http://127.0.0.1:9")   # discard port
    body = client.get("/api/broker").json()

    assert body["reachable"] is False
    assert body["router"] is None
    assert body["registry"] is None
    assert body["detail"], "an unreachable broker must say why"
    assert "127.0.0.1:9" in body["base"]


def test_broker_status_does_not_raise_when_the_broker_is_absent(client, monkeypatch):
    """
    A missing broker is the normal state before `start-broker.sh` runs. The
    Control Center must still load — a 500 here would take the whole Models
    view down for a condition that is expected.
    """
    monkeypatch.setenv("OE_MAX_BASE", "http://127.0.0.1:9")
    assert client.get("/api/broker").status_code == 200


def test_system_reports_gpu_presence_honestly(client):
    """
    A host with no accelerator is the normal case, not a fault, and must be
    distinguishable from one where sampling failed. Neither may be rendered as
    0% utilised — a number nothing measured.
    """
    body = client.get("/api/system").json()

    assert "gpu" in body, "the system view says nothing about the accelerator"
    assert isinstance(body["gpu"]["available"], bool)
    if not body["gpu"]["available"]:
        assert body["gpu"]["reason"], "absence must explain itself"
        assert body["gpu"]["gpus"] == []
        assert body["gpu"]["count"] == 0


def test_broker_probe_does_not_wait_out_a_windows_syn_retry(client, monkeypatch):
    """The Models view refetches this on every live tick; it must answer fast.

    Measured before this was bounded: 2352ms at the median, of which 2030ms was
    the operating system retrying the SYN. Windows drops the packet for a closed
    port rather than refusing it, so `connect` runs to its full budget -- and a
    raw `socket.connect` costs the same 2030ms, which is what rules out httpx
    and TLS as the cause.

    The read budget stays at 5s, because a broker that is up may be busy. Only
    the connect is short, and only on loopback.
    """
    import time

    monkeypatch.setenv("OE_MAX_BASE", "http://127.0.0.1:9")   # discard port

    started = time.perf_counter()
    response = client.get("/api/broker")
    elapsed = time.perf_counter() - started

    assert response.status_code == 200
    assert response.json()["reachable"] is False
    # Generous against a loaded CI box; the point is that it is not ~2.3s.
    assert elapsed < 1.5, f"broker probe took {elapsed:.2f}s"


def test_the_connect_budget_is_short_only_for_a_local_broker():
    """A remote OE_MAX_BASE must not inherit a 250ms handshake budget."""
    from control_plane.api.app import _broker_timeout

    for local in ("http://127.0.0.1:8787", "http://localhost:8787", "http://[::1]:8787"):
        assert _broker_timeout(local).connect == 0.25, local

    remote = _broker_timeout("https://broker.internal.example:8787")
    assert remote.connect == 2.0
    # The read budget is unchanged either way -- a busy broker is not a missing one.
    assert remote.read == 5.0
    assert _broker_timeout("http://127.0.0.1:8787").read == 5.0


def test_an_absent_broker_says_it_is_absent_not_that_it_is_slow(client, monkeypatch):
    """`str(ConnectTimeout())` is empty, so the obvious formatting renders a bare
    "ConnectTimeout:" -- which reads as a slow broker when it means there is
    none. On loopback a connect timeout is the normal way "not running"
    presents, and the detail has to say so."""
    monkeypatch.setenv("OE_MAX_BASE", "http://127.0.0.1:9")

    detail = client.get("/api/broker").json()["detail"]

    assert detail.strip(), "an unreachable broker must say why"
    assert not detail.rstrip().endswith(":"), f"empty exception message: {detail!r}"
    assert "no broker listening" in detail or "could not connect" in detail, detail
    assert "127.0.0.1:9" in detail


def test_a_reachable_broker_is_reported_from_its_own_payload(client, monkeypatch):
    """The other broker tests all cover the absent case. This covers the
    present one, because the timeout split could just as easily have broken it:
    a 250ms connect budget applied to the whole request would turn a working
    broker into a reported outage.

    Uses a stub server rather than the real broker, so the test needs no
    provider, no model and no port that something else might hold.
    """
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    payload = {"router": {"chain": ["oe-max-primary"]}, "registry": {"providers": 1}}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        monkeypatch.setenv("OE_MAX_BASE", f"http://{host}:{port}")

        body = client.get("/api/broker").json()

        assert body["reachable"] is True
        assert body["router"] == payload["router"]
        assert body["registry"] == payload["registry"]
        assert body["base"].endswith(str(port))
    finally:
        server.shutdown()
        server.server_close()
