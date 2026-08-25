"""
The OpenCode non-interference boundary is a hard requirement, so it gets tests
that assert the *negative*: Evolution must be unable to reach operator state.
"""
import os
import pytest
from control_plane.sandbox.opencode import (
    IsolationLevel, OpenCodeIsolation, _forbidden_roots,
)


def test_all_owned_paths_live_under_the_workspace(workspace):
    iso = OpenCodeIsolation(workspace)
    for key, path in iso.ensure_layout().items():
        assert os.path.abspath(path).startswith(os.path.abspath(workspace)), key


def test_owned_paths_never_overlap_operator_state(workspace):
    iso = OpenCodeIsolation(workspace)
    for path in iso.ensure_layout().values():
        for forbidden in _forbidden_roots():
            assert not os.path.abspath(path).startswith(os.path.abspath(forbidden))


def test_writing_into_operator_state_is_refused(workspace):
    iso = OpenCodeIsolation(workspace)
    with pytest.raises(PermissionError):
        iso._assert_safe(os.path.join(os.path.expanduser("~"), ".config", "opencode", "x"))


def test_env_redirects_home_and_every_xdg_path(workspace):
    env = OpenCodeIsolation(workspace).env()
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                "XDG_CACHE_HOME", "XDG_STATE_HOME"):
        assert env[key].startswith(os.path.abspath(workspace)), key


def test_env_drops_inherited_opencode_variables(workspace, monkeypatch):
    """
    Overwriting is not enough — a stale inherited value must not survive at all,
    or a child could be pointed back at the operator's installation.
    """
    monkeypatch.setenv("OPENCODE_CONFIG", "/home/operator/.config/opencode")
    monkeypatch.setenv("OPENCODE_HOME", "/home/operator/.opencode")
    env = OpenCodeIsolation(workspace).env()
    assert "OPENCODE_CONFIG" not in env
    assert "OPENCODE_HOME" not in env
    assert env["OPENCODE_CONFIG_DIR"].startswith(os.path.abspath(workspace))


def test_preflight_reports_unavailable_without_a_binary(workspace, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda *_a, **_k: None)
    report = OpenCodeIsolation(workspace).preflight()
    assert report.level is IsolationLevel.UNAVAILABLE
    assert report.ok is False
    assert any("No OpenCode binary" in r for r in report.reasons)
    # Disabling the backend must be presented as the documented fallback,
    # not as a failure — the operator's own OpenCode is never touched instead.
    assert any("native openevolve" in w.lower() for w in report.warnings)


def test_status_is_json_serialisable(workspace):
    import json
    json.dumps(OpenCodeIsolation(workspace).status())


def test_project_config_written_inside_the_tree(workspace):
    iso = OpenCodeIsolation(workspace)
    path = iso.write_project_config({"model": "test"})
    assert os.path.abspath(path).startswith(os.path.abspath(workspace))
    assert os.path.exists(path)


def test_omo_detection_never_raises_when_absent(workspace, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda *_a, **_k: None)
    omo = OpenCodeIsolation(workspace).detect_omo()
    assert omo["available"] is False
    assert len(omo["checked"]) >= 3   # several names probed, none hardcoded as THE name
