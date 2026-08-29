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


# -- status caching ---------------------------------------------------------
#
# The System Health page polls /api/system every five seconds, and the isolation
# check inside it cost 874 ms a call: `opencode --version` at 553 ms and a
# container-runtime probe at 256 ms, both answering questions that do not change
# between polls. The endpoint went from 959 ms to 146 ms.


def test_status_is_cached_between_calls(workspace, monkeypatch):
    from control_plane.sandbox import opencode as oc

    monkeypatch.setattr(oc, "_STATUS_CACHE", {})
    calls = []
    real = oc.OpenCodeIsolation.preflight

    def counting(self):
        calls.append(1)
        return real(self)

    monkeypatch.setattr(oc.OpenCodeIsolation, "preflight", counting)

    iso = OpenCodeIsolation(workspace)
    first = iso.status()
    second = iso.status()

    assert len(calls) == 1, "the second call re-ran the subprocess probes"
    assert second == first


def test_a_zero_max_age_forces_a_fresh_check(workspace, monkeypatch):
    """An operator who has just installed OpenCode should not wait out the TTL."""
    from control_plane.sandbox import opencode as oc

    monkeypatch.setattr(oc, "_STATUS_CACHE", {})
    calls = []
    real = oc.OpenCodeIsolation.preflight
    monkeypatch.setattr(
        oc.OpenCodeIsolation, "preflight",
        lambda self: (calls.append(1), real(self))[1],
    )

    iso = OpenCodeIsolation(workspace)
    iso.status()
    iso.status(max_age=0)

    assert len(calls) == 2


def test_checked_at_is_the_check_not_the_call(workspace, monkeypatch):
    """A cached answer must not claim to be fresh.

    Stamping `checked_at` on the way out would make a 30-second-old result look
    current, which is worse than being slow -- the field exists so a reader can
    see how stale the answer is.
    """
    from control_plane.sandbox import opencode as oc

    monkeypatch.setattr(oc, "_STATUS_CACHE", {})
    iso = OpenCodeIsolation(workspace)
    first = iso.status()
    second = iso.status()

    assert second["checked_at"] == first["checked_at"]


def test_the_cache_is_per_workspace(workspace, tmp_path, monkeypatch):
    """Two workspaces are two different answers; one must not serve the other."""
    from control_plane.sandbox import opencode as oc

    monkeypatch.setattr(oc, "_STATUS_CACHE", {})
    other = str(tmp_path / "other-workspace")
    os.makedirs(other, exist_ok=True)

    a = OpenCodeIsolation(workspace).status()
    b = OpenCodeIsolation(other).status()

    assert a["root"] != b["root"]
    assert len(oc._STATUS_CACHE) == 2


def test_preflight_itself_is_not_cached(workspace, monkeypatch):
    """`preflight()` calls `ensure_layout()`, which has side effects a caller may
    rely on, and is the entry point for when the answer must be current."""
    from control_plane.sandbox import opencode as oc

    monkeypatch.setattr(oc, "_STATUS_CACHE", {})
    iso = OpenCodeIsolation(workspace)
    iso.status()

    import shutil as _shutil

    _shutil.rmtree(iso.root, ignore_errors=True)
    iso.preflight()
    assert os.path.isdir(iso.root), "preflight should have recreated the layout"


def test_a_cached_status_is_a_copy(workspace, monkeypatch):
    """A caller mutating the returned dict must not corrupt what the next one
    gets -- the API hands this straight to a JSON serialiser."""
    from control_plane.sandbox import opencode as oc

    monkeypatch.setattr(oc, "_STATUS_CACHE", {})
    iso = OpenCodeIsolation(workspace)
    first = iso.status()
    first["level"] = "tampered"

    assert iso.status()["level"] != "tampered"
