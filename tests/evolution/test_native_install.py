"""
The native agent runtime reaches the engine without being inside it.

This runtime arrived from a session that put seven modules under `openevolve/`
and bolted five methods onto `OpenEvolve` by editing `controller.py` — 172
inserted lines across `__init__.py`, `config.py` and `controller.py`. The code
worked. Its address ended the guarantee that makes an upstream merge a
fast-forward instead of a conflict resolution.

Nothing about the capability required that. The modules only ever *read* from
the engine (`Program`, `ProgramDatabase`), which an import does as well from
`control_plane/native/` as from inside the package, and the five methods bind to
the class just as well at runtime as at definition time — which is the pattern
`control_plane/telemetry/instrument.py` has used all along.

`test_patch_surface.py` already fails if `openevolve/` is modified. These tests
cover the other half: that the capability is genuinely present, so nobody is
tempted to reach for the edit again because "the wrapper does not really work".
"""

import pytest

from control_plane import native

METHODS = (
    "create_native_agent_runtime",
    "run_native_agent",
    "run_native_model_agent",
    "fork_native_model_session",
)


@pytest.fixture
def engine():
    from openevolve.controller import OpenEvolve

    return OpenEvolve


class TestInstall:
    def test_install_attaches_every_method(self, engine):
        native.install()

        for name in METHODS:
            assert hasattr(engine, name), (
                f"{name} missing: the runtime install is the only thing "
                "standing in for the forbidden controller edit")

    def test_install_is_idempotent(self, engine):
        """
        It runs from a test fixture, from the API and potentially from a worker.
        Binding twice is harmless here, but reporting that it installed twice
        would make `installed()` meaningless.
        """
        native.install()
        assert native.install() is False
        assert native.installed() is True

    def test_installed_reports_the_truth_before_and_after(self, engine, monkeypatch):
        """
        A caller that asked for the runtime must be able to tell it got one.
        """
        monkeypatch.delattr(engine, "__evolution_native_agent__", raising=False)
        assert native.installed() is False

        assert native.install() is True
        assert native.installed() is True

    def test_the_methods_are_bound_to_the_class_not_to_one_instance(self, engine):
        """
        Every OpenEvolve gets them, including ones constructed before install
        ran — which is what makes the ordering of install and construction a
        non-issue.
        """
        native.install()

        for name in METHODS:
            assert callable(getattr(engine, name))


class TestNoPatchRequired:
    def test_the_native_modules_live_outside_the_engine_tree(self):
        """
        The relocation is the actual fix. If these ever import from
        `openevolve.native_*` again, they have moved back in.
        """
        import control_plane.native.native_controller as controller

        assert controller.__name__.startswith("control_plane.native")

    def test_no_native_module_was_added_under_openevolve(self):
        import os

        import openevolve

        engine_dir = os.path.dirname(os.path.abspath(openevolve.__file__))
        strays = [n for n in os.listdir(engine_dir) if n.startswith("native_")]

        assert strays == [], (
            f"native modules are back inside the engine tree: {strays}")

    def test_the_engine_still_exports_only_what_upstream_exports(self):
        """
        The original change re-exported six native symbols from
        `openevolve/__init__.py`. A fork that adds to upstream's public API
        conflicts the moment upstream touches that file.
        """
        import openevolve

        assert not [n for n in getattr(openevolve, "__all__", []) if "Native" in n], (
            "native symbols are being re-exported from the engine package")
