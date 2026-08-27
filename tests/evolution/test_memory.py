"""
Project memory: the journal, the derived digest, and the log importer.

The design rule under test throughout is the one the storage layer is built
on: anything derivable is derived, and only what cannot be reconstructed from
the event log is stored. A summary table that drifts from the events it
summarises is worse than no summary, because it is believed.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from control_plane.memory import Journal, build_digest
from control_plane.memory.importer import discover_logs, import_all, import_log
from control_plane.memory.resume import _infer_task, _output_dir_for, render_text
from control_plane.storage.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "cp.db"))
    yield s
    s.close()


# -- journal ---------------------------------------------------------------


def test_an_entry_survives_a_round_trip(store):
    Journal(store).add("switched primary", kind="decision",
                       detail="Ox Alpha withdrawn", tags=["providers"])

    got = Journal(store).list()[0]

    assert got.title == "switched primary"
    assert got.kind == "decision"
    assert got.tags == ["providers"]
    assert got.source == "user"


def test_a_blank_title_is_refused(store):
    """
    An untitled entry is invisible in every listing that exists, so accepting
    one would be a silent discard wearing the costume of a successful write.
    """
    for blank in ("", "   ", "\n"):
        with pytest.raises(ValueError):
            Journal(store).add(blank)


def test_an_unknown_kind_is_filed_rather_than_rejected(store):
    """Losing a thought to a mislabel is worse than filing it imprecisely."""
    assert Journal(store).add("x", kind="not-a-kind").kind == "note"


def test_who_wrote_it_is_preserved(store):
    """
    A person's assertion and a program's inference must stay distinguishable,
    or the journal is untrustworthy for exactly the decisions it holds.
    """
    j = Journal(store)
    j.add("measured", source="agent")
    j.add("decided", source="user")

    by_source = {e.title: e.source for e in j.list()}

    assert by_source == {"measured": "agent", "decided": "user"}


def test_newest_first(store):
    j = Journal(store)
    j.add("older", created_at=1000.0)
    j.add("newer", created_at=2000.0)

    assert [e.title for e in j.list()] == ["newer", "older"]


def test_search_covers_detail_not_just_title(store):
    Journal(store).add("a title", detail="the needle is in here")

    assert len(Journal(store).search("needle")) == 1


def test_a_corrupt_tags_column_costs_that_field_only(store):
    """One hand-edited row must not take down the whole listing."""
    entry = Journal(store).add("fine")
    store.execute("UPDATE journal SET tags = ? WHERE entry_id = ?",
                  ("{not json", entry.entry_id))

    got = Journal(store).list()

    assert len(got) == 1 and got[0].title == "fine" and got[0].tags == []


def test_delete_reports_whether_it_existed(store):
    e = Journal(store).add("temporary")

    assert Journal(store).delete(e.entry_id) is True
    assert Journal(store).delete(e.entry_id) is False


# -- digest ----------------------------------------------------------------


def test_an_empty_workspace_produces_an_empty_digest_not_a_plausible_one(store):
    d = build_digest(store)

    assert d["totals"]["runs"] == 0
    assert d["resumable"] == []
    assert d["best_ever"] is None
    # None, not 0. "No data" and "zero iterations across four runs" are
    # different facts and must not render the same.
    assert d["totals"]["iterations"] is None
    assert "No runs recorded" in render_text(d)


def test_a_run_without_a_checkpoint_is_not_offered_as_resumable(store):
    store.execute(
        "INSERT INTO experiments(experiment_id, name, created_at)"
        " VALUES ('e1','e',1000.0)")
    store.execute(
        "INSERT INTO runs(run_id, experiment_id, status, iterations_done)"
        " VALUES ('r1','e1','completed',3)")

    d = build_digest(store)

    assert d["totals"]["runs"] == 1
    assert d["resumable"] == []
    assert "no run has written a checkpoint" in render_text(d).lower()


def test_a_checkpointed_run_comes_with_a_command_to_resume_it(store):
    store.execute("INSERT INTO experiments(experiment_id, name, created_at)"
                  " VALUES ('e1','e',1000.0)")
    store.execute(
        "INSERT INTO runs(run_id, experiment_id, status, best_fitness, output_dir)"
        " VALUES ('r1','e1','completed', 1.5,"
        " 'runs/20260826-001122-circle_packing-max')")
    store.execute(
        "INSERT INTO checkpoints(checkpoint_id, run_id, iteration, path, status)"
        " VALUES ('c1','r1',6,"
        " 'runs/20260826-001122-circle_packing-max/checkpoints/checkpoint_6','ok')")

    point = build_digest(store)["resumable"][0]

    assert point["checkpoint_iteration"] == 6
    assert point["task"] == "circle_packing"
    assert "resume-evolution.sh" in point["resume_command"]
    assert "--task circle_packing" in point["resume_command"]


def test_the_output_directory_is_recovered_when_it_was_never_recorded():
    """
    `runs.output_dir` was NULL for every run until the started-event handler
    was fixed to store it. History from before that fix has only the
    checkpoint path, and deriving from it is what keeps those runs resumable.
    """
    run = {"output_dir": None, "checkpoint_dir": None}
    got = _output_dir_for(run, "runs/all-features/checkpoints/checkpoint_6")

    assert got == "runs/all-features"


def test_a_recorded_output_directory_wins_over_the_derived_one():
    run = {"output_dir": "runs/explicit"}
    assert _output_dir_for(run, "runs/derived/checkpoints/checkpoint_1") == "runs/explicit"


@pytest.mark.parametrize("directory,expected", [
    ("runs/20260826-001122-circle_packing-max", "circle_packing"),
    ("runs/20260826-001122-function_minimization-stock", "function_minimization"),
    ("runs/hand-named", None),
    (None, None),
])
def test_task_inference_declines_to_guess(directory, expected):
    """
    Resuming with the wrong evaluator produces confident scores for a
    different problem, so an unrecognised directory yields None rather than a
    plausible default.
    """
    assert _infer_task(directory) == expected


def test_the_best_ever_ignores_the_date_window(store):
    """
    Someone back after a month wants the high-water mark. Hiding it behind a
    window would answer a question they did not ask.
    """
    store.execute("INSERT INTO experiments(experiment_id, name, created_at)"
                  " VALUES ('e1','e',1000.0)")
    store.execute(
        "INSERT INTO runs(run_id, experiment_id, status, best_fitness, ended_at)"
        " VALUES ('old','e1','completed', 9.9, ?)", (time.time() - 400 * 86400,))

    d = build_digest(store, window_days=7)

    assert d["recent_runs"] == []
    assert d["best_ever"]["best_fitness"] == 9.9


# -- importer --------------------------------------------------------------


def _write_log(path, run_id="r_import"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    events = [
        {"event_id": f"{run_id}-ev{i}", "type": "candidate.created",
         "component": "database", "run_id": run_id, "timestamp": 1000.0 + i,
         "candidate_id": f"c{i}", "status": "ok", "metadata": {}, "metrics": {}}
        for i in range(3)
    ]
    with open(path, "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")
    return path


def test_a_shell_launched_run_reaches_the_database(store, tmp_path):
    """
    The collector only ingests while the Control Center is up, so without the
    importer a CLI-launched run left no trace in its own project's history.
    """
    log = _write_log(str(tmp_path / "runs" / "a" / "events.ndjson"))

    assert import_log(store, log)["events"] == 3
    assert store.query_one("SELECT COUNT(*) AS n FROM events")["n"] == 3


def test_importing_twice_writes_nothing_the_second_time(store, tmp_path):
    log = _write_log(str(tmp_path / "runs" / "a" / "events.ndjson"))
    import_log(store, log)

    again = import_log(store, log)

    assert again["events"] == 0
    assert again.get("skipped") is True


def test_a_growing_log_is_resumed_from_the_offset(store, tmp_path):
    """A run still appending must be importable while it runs."""
    log = _write_log(str(tmp_path / "runs" / "a" / "events.ndjson"))
    import_log(store, log)

    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "event_id": "ev-late", "type": "candidate.created",
            "component": "database", "run_id": "r_import", "timestamp": 2000.0,
            "candidate_id": "cX", "status": "ok", "metadata": {}, "metrics": {},
        }) + "\n")

    assert import_log(store, log)["events"] == 1


def test_a_torn_final_line_is_left_for_the_next_pass(store, tmp_path):
    """
    A live writer can be mid-line. Consuming a partial line would drop the
    event permanently once the writer completed it.
    """
    log = _write_log(str(tmp_path / "runs" / "a" / "events.ndjson"))
    with open(log, "a", encoding="utf-8") as fh:
        fh.write('{"event_id": "partial"')          # no newline

    import_log(store, log)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(', "type": "candidate.created", "component": "database",'
                 ' "run_id": "r_import", "timestamp": 3000.0,'
                 ' "candidate_id": "cP", "status": "ok",'
                 ' "metadata": {}, "metrics": {}}\n')

    assert import_log(store, log)["events"] == 1


def test_a_truncated_log_is_re_read_rather_than_resumed_into(store, tmp_path):
    """A stale offset must degrade to re-reading, never to corruption."""
    log = _write_log(str(tmp_path / "runs" / "a" / "events.ndjson"))
    import_log(store, log)

    _write_log(log, run_id="r_replaced")            # shorter/rewritten file

    assert import_log(store, log)["events"] >= 0    # must not raise


def test_an_unreadable_log_does_not_stop_the_others(store, tmp_path):
    outcome = import_log(store, str(tmp_path / "nope" / "events.ndjson"))

    assert outcome["skipped"] is True
    assert "error" in outcome


def test_discovery_finds_run_logs(tmp_path, monkeypatch):
    _write_log(str(tmp_path / "runs" / "a" / "events.ndjson"))
    _write_log(str(tmp_path / "runs" / "b" / "events.ndjson"))
    monkeypatch.chdir(tmp_path)

    assert len(discover_logs()) == 2


def test_import_all_reports_what_it_did(store, tmp_path, monkeypatch):
    _write_log(str(tmp_path / "runs" / "a" / "events.ndjson"), run_id="ra")
    _write_log(str(tmp_path / "runs" / "b" / "events.ndjson"), run_id="rb")
    monkeypatch.chdir(tmp_path)

    summary = import_all(store)

    assert summary["logs_found"] == 2
    assert summary["files_updated"] == 2
    assert summary["events"] == 6


def test_a_run_with_no_timestamp_is_shown_rather_than_hidden(store):
    """
    Coalescing a missing timestamp to epoch put such runs before every window
    and made them vanish — including from the resumable list, which is the one
    thing this view exists to produce. A run in the database is a run that
    happened; not knowing when is a reason to show it, not to hide it.
    """
    store.execute("INSERT INTO experiments(experiment_id, name, created_at)"
                  " VALUES ('e1','e',1000.0)")
    store.execute(
        "INSERT INTO runs(run_id, experiment_id, status) VALUES ('r_notime','e1','completed')")

    d = build_digest(store, window_days=1)

    assert [r["run_id"] for r in d["recent_runs"]] == ["r_notime"]


def test_the_date_window_still_excludes_an_old_timestamped_run(store):
    """The guard against over-applying the fix above."""
    store.execute("INSERT INTO experiments(experiment_id, name, created_at)"
                  " VALUES ('e1','e',1000.0)")
    store.execute(
        "INSERT INTO runs(run_id, experiment_id, status, ended_at)"
        " VALUES ('r_old','e1','completed', ?)", (time.time() - 400 * 86400,))

    assert build_digest(store, window_days=7)["recent_runs"] == []
