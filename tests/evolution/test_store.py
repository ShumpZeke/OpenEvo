"""Projections must be derived, idempotent, and rebuildable from the event log."""
import json, os
import pytest
from control_plane.storage.store import Store
from control_plane.telemetry.events import Component, Event, EventType, Status


@pytest.fixture
def store(workspace):
    s = Store(os.path.join(workspace, "t.db"))
    yield s
    s.close()


def cand(run, cid, score, gen=0, island=0, parent=None, cell="1-1"):
    return Event(
        type=EventType.CANDIDATE_CREATED, component=Component.DATABASE,
        run_id=run, candidate_id=cid, generation=gen, island_id=island,
        metrics={"combined_score": score},
        metadata={"parent_id": parent}, output={"code": f"x = {score}"},
    )


def cell(run, cid, score, island=0, key="1-1"):
    return Event(
        type=EventType.MAP_ELITES_CELL_UPDATED, component=Component.DATABASE,
        run_id=run, candidate_id=cid, island_id=island,
        metrics={"score": score},
        metadata={"cell_key": key, "coords": [1, 1], "dimensions": ["a", "b"]},
    )


def test_ingest_is_idempotent(store):
    evs = [cand("r1", "c1", 0.5)]
    assert store.ingest(evs) == 1
    assert store.ingest(evs) == 0     # replaying the log must not double-count
    assert store.query("SELECT COUNT(*) c FROM candidates")[0]["c"] == 1


def test_candidate_projection(store):
    store.ingest([cand("r1", "c1", 0.75, gen=3, island=2, parent="c0")])
    row = store.query_one("SELECT * FROM candidates WHERE candidate_id='c1'")
    assert row["combined_score"] == 0.75
    assert row["generation"] == 3 and row["island_id"] == 2
    assert row["parent_id"] == "c0"
    edges = store.query("SELECT * FROM candidate_parents WHERE candidate_id='c1'")
    assert edges[0]["parent_id"] == "c0"


def test_map_elites_cells_are_scoped_per_island(store):
    """
    Upstream keeps one feature map PER ISLAND, so the same cell key on two
    islands is two distinct cells. Collapsing them would show the wrong elite.
    """
    store.ingest([
        cell("r1", "c1", 0.5, island=0, key="2-2"),
        cell("r1", "c2", 0.9, island=1, key="2-2"),
    ])
    rows = store.query("SELECT * FROM map_elites_cells ORDER BY island_id")
    assert len(rows) == 2
    assert {r["island_id"] for r in rows} == {0, 1}
    assert {r["candidate_id"] for r in rows} == {"c1", "c2"}


def test_cell_replacement_records_history(store):
    store.ingest([cell("r1", "c1", 0.4, key="3-3")])
    store.ingest([cell("r1", "c2", 0.8, key="3-3")])
    now = store.query_one("SELECT * FROM map_elites_cells WHERE cell_key='3-3'")
    assert now["candidate_id"] == "c2"
    assert now["replacements"] == 1
    hist = store.query("SELECT * FROM map_elites_history WHERE cell_key='3-3' ORDER BY id")
    assert len(hist) == 2
    assert hist[1]["previous_candidate_id"] == "c1"


def test_best_updates_are_exclusive(store):
    store.ingest([cand("r1", "c1", 0.5), cand("r1", "c2", 0.9)])
    store.ingest([Event(type=EventType.CANDIDATE_BEST_UPDATED,
                        component=Component.DATABASE, run_id="r1", candidate_id="c1",
                        metrics={"combined_score": 0.5})])
    store.ingest([Event(type=EventType.CANDIDATE_BEST_UPDATED,
                        component=Component.DATABASE, run_id="r1", candidate_id="c2",
                        metrics={"combined_score": 0.9})])
    best = store.query("SELECT candidate_id FROM candidates WHERE is_best=1")
    assert [b["candidate_id"] for b in best] == ["c2"]


def test_model_request_projection_computes_throughput(store):
    store.ingest([Event(
        type=EventType.MODEL_REQUEST_COMPLETED, component=Component.LLM,
        run_id="r1", duration_ms=2000.0,
        metrics={"total_tokens": 1000, "prompt_tokens": 600, "completion_tokens": 400},
        metadata={"request_id": "m1", "provider": "opencode_zen", "model": "x-preview-f-free"},
    )])
    row = store.query_one("SELECT * FROM model_requests WHERE request_id='m1'")
    assert row["total_tokens"] == 1000
    assert row["tokens_per_sec"] == 500.0
    assert row["status"] == "ok"


def test_rate_limited_requests_are_flagged(store):
    store.ingest([Event(
        type=EventType.MODEL_RATE_LIMITED, component=Component.LLM, run_id="r1",
        status=Status.FAILED, metadata={"request_id": "m2", "provider": "opencode_zen"},
    )])
    row = store.query_one("SELECT * FROM model_requests WHERE request_id='m2'")
    assert row["rate_limited"] == 1


def test_migration_projection_updates_island_counters(store):
    store.ingest([Event(
        type=EventType.ISLAND_MIGRATION_COMPLETED, component=Component.DATABASE,
        run_id="r1", metadata={"migrations": [
            {"source_island": 0, "target_island": 1, "candidate_id": "c1",
             "new_candidate_id": "c1m"},
        ]},
    )])
    assert store.query("SELECT COUNT(*) c FROM migrations")[0]["c"] == 1


def test_evaluation_lifecycle_merges_into_one_row(store):
    started = Event(type=EventType.EVALUATOR_STARTED, component=Component.EVALUATOR,
                    run_id="r1", candidate_id="c1",
                    metadata={"evaluation_id": "e1", "evaluator_id": "ev.py"})
    done = Event(type=EventType.EVALUATOR_COMPLETED, component=Component.EVALUATOR,
                 run_id="r1", candidate_id="c1", duration_ms=120.0,
                 metrics={"combined_score": 0.7}, metadata={"evaluation_id": "e1"})
    store.ingest([started, done])
    rows = store.query("SELECT * FROM evaluations")
    assert len(rows) == 1
    assert rows[0]["status"] == "ok" and rows[0]["combined_score"] == 0.7


def test_search_index_finds_candidate_code(store):
    store.ingest([cand("r1", "c1", 0.5)])
    hits = store.query(
        "SELECT entity_id FROM search_index WHERE search_index MATCH ?", ("candidate",))
    assert any(h["entity_id"] == "c1" for h in hits)


def test_rebuild_from_log_reconstructs_projections(store, workspace):
    path = os.path.join(workspace, "log.ndjson")
    evs = [cand("r1", f"c{i}", i / 10) for i in range(10)] + [cell("r1", "c9", 0.9)]
    with open(path, "w") as fh:
        for e in evs:
            fh.write(json.dumps(e.to_dict()) + "\n")
    store.ingest(evs)
    before = store.query("SELECT COUNT(*) c FROM candidates")[0]["c"]

    store.rebuild_projections_from_log(path)
    after = store.query("SELECT COUNT(*) c FROM candidates")[0]["c"]
    assert before == after == 10
    assert store.query("SELECT COUNT(*) c FROM map_elites_cells")[0]["c"] == 1


def test_torn_final_line_does_not_abort_rebuild(store, workspace):
    """A killed process can leave a half-written line; it must not lose the rest."""
    path = os.path.join(workspace, "torn.ndjson")
    with open(path, "w") as fh:
        fh.write(json.dumps(cand("r1", "c1", 0.5).to_dict()) + "\n")
        fh.write('{"type": "candidate.created", "compo')  # truncated
    store.rebuild_projections_from_log(path)
    assert store.query("SELECT COUNT(*) c FROM candidates")[0]["c"] == 1


def test_eval_status_backfills_when_evaluation_precedes_candidate(store):
    """
    Upstream evaluates a program BEFORE adding it to the database, so evaluator
    events arrive before candidate.created. Projections must still end up with
    the candidate marked evaluated rather than stuck at 'pending'.
    """
    from control_plane.telemetry.events import Status
    store.ingest([
        Event(type=EventType.EVALUATOR_STARTED, component=Component.EVALUATOR,
              run_id="r1", candidate_id="c1", metadata={"evaluation_id": "e1"}),
        Event(type=EventType.EVALUATOR_COMPLETED, component=Component.EVALUATOR,
              run_id="r1", candidate_id="c1", duration_ms=10.0,
              metrics={"combined_score": 0.66}, metadata={"evaluation_id": "e1"}),
    ])
    # candidate.created arrives afterwards, as it does in a real run
    store.ingest([cand("r1", "c1", 0.66)])
    row = store.query_one("SELECT * FROM candidates WHERE candidate_id='c1'")
    assert row["eval_status"] == "ok", "evaluated candidate must not read as pending"
    assert row["combined_score"] == 0.66


# ---------------------------------------------------------------------------
# Orphaned runs
#
# A run killed by a crash, a container restart or `kill -9` never emits its own
# stopped event, so the projection keeps reporting it as running — forever, and
# in the Control Center's run list. The operator reads "running" and waits for
# output that will never come, which is worse than a blank.
# ---------------------------------------------------------------------------

def _running_run(store, run_id, pid):
    from control_plane.telemetry.events import Component, Event, EventType

    store.ingest([Event(
        type=EventType.EXPERIMENT_CREATED, component=Component.CONTROL_PLANE,
        run_id=run_id, experiment_id=f"exp_{run_id}", metadata={"name": run_id})])
    store.ingest([Event(
        type=EventType.EXPERIMENT_STARTED, component=Component.CONTROLLER,
        run_id=run_id, experiment_id=f"exp_{run_id}", pid=pid,
        summary="evolution run started")])


def _status(store, run_id):
    return store.query_one("SELECT status, error, ended_at FROM runs WHERE run_id=?",
                           (run_id,))


def test_a_run_whose_process_is_gone_is_marked_failed(tmp_path):
    from control_plane.storage.store import Store

    store = Store(str(tmp_path / "cp.db"))
    try:
        _running_run(store, "run_dead", pid=999_999)   # no such process
        assert _status(store, "run_dead")["status"] == "running"

        reconciled = store.reconcile_orphaned_runs()

        assert [r["run_id"] for r in reconciled] == ["run_dead"]
        row = _status(store, "run_dead")
        assert row["status"] == "failed"
        assert "never reported an end" in row["error"]
        assert row["ended_at"] is not None
    finally:
        store.close()


def test_a_live_engine_process_is_left_alone(tmp_path):
    """
    A real running engine must survive reconciliation, or a reconnect would
    kill the status of the run the operator is actually watching.

    Uses a real subprocess whose command line looks like the engine's, because
    the predicate reads /proc — a mock would test the mock.
    """
    import subprocess
    import sys
    import time as _time

    from control_plane.storage.store import Store, _process_alive

    proc = subprocess.Popen(
        [sys.executable, "-c",
         "import time; __name__ = 'control_plane.runner.entrypoint'; time.sleep(30)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    store = Store(str(tmp_path / "cp.db"))
    try:
        # The marker has to be in argv, which is what /proc/<pid>/cmdline shows.
        assert "control_plane.runner.entrypoint" in " ".join(proc.args)
        _time.sleep(0.2)
        assert _process_alive(proc.pid) is True

        _running_run(store, "run_live", pid=proc.pid)
        assert store.reconcile_orphaned_runs() == []
        assert _status(store, "run_live")["status"] == "running"
    finally:
        proc.kill()
        proc.wait(timeout=5)
        store.close()


def test_a_recycled_pid_does_not_keep_a_dead_run_alive():
    """
    Signal 0 alone is not enough: PIDs are reused, and a recycled one would
    keep a dead run marked running indefinitely — the exact failure being
    fixed.
    """
    import os

    from control_plane.storage.store import _process_alive

    # This test process is alive but is pytest, not an engine entrypoint.
    assert _process_alive(os.getpid()) is False
    assert _process_alive(-1) is False


def test_a_run_with_no_recorded_pid_is_not_guessed_at(tmp_path):
    """
    Every Event stamps its own pid, so this cannot arrive through the normal
    path — but a row written any other way must not be judged on a pid that is
    not there. Whether a process on another machine is alive is unknowable
    from here, and unknowable is left alone.
    """
    from control_plane.storage.store import Store

    store = Store(str(tmp_path / "cp.db"))
    try:
        _running_run(store, "run_nopid", pid=999_999)
        store._conn.execute("UPDATE runs SET pid = NULL WHERE run_id = 'run_nopid'")
        store._conn.commit()

        assert store.reconcile_orphaned_runs() == []
        assert _status(store, "run_nopid")["status"] == "running"
    finally:
        store.close()


def test_a_finished_run_is_not_re_marked(tmp_path):
    from control_plane.storage.store import Store
    from control_plane.telemetry.events import Component, Event, EventType

    store = Store(str(tmp_path / "cp.db"))
    try:
        _running_run(store, "run_done", pid=999_999)
        store.ingest([Event(
            type=EventType.EXPERIMENT_COMPLETED, component=Component.CONTROLLER,
            run_id="run_done", experiment_id="exp_run_done", pid=999_999,
            summary="completed")])
        assert store.reconcile_orphaned_runs() == []
        assert _status(store, "run_done")["status"] == "completed"
    finally:
        store.close()


def test_reconciling_twice_is_idempotent(tmp_path):
    from control_plane.storage.store import Store

    store = Store(str(tmp_path / "cp.db"))
    try:
        _running_run(store, "run_dead", pid=999_999)
        assert len(store.reconcile_orphaned_runs()) == 1
        assert store.reconcile_orphaned_runs() == []
    finally:
        store.close()
