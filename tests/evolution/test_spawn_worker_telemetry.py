"""
Worker telemetry has to survive `spawn`, not only `fork`.

Upstream hands `ProcessPoolExecutor` a plain module-level reference as its
initializer (`openevolve/process_parallel.py`: `"initializer": _worker_init`).
`install_worker_hook` rebinds that module attribute to a wrapper which installs
telemetry in the child.

Under `fork` the child inherits the parent's patched module, so the wrapper is
there and every worker-side event is emitted. Under `spawn` the initializer is
pickled **by reference** and re-resolved by importing `process_parallel` fresh
in the child -- which yields upstream's original function and drops the wrapper
on the floor. Nothing errors. The run still produces candidates, because those
travel back on the returned Program objects, so the only symptom is that
`model_requests`, `tokens` and `iterations_done` all read 0.

That is a plausible-looking zero on every Windows and macOS run, where `spawn`
is the default context, and a plausible-looking zero is precisely what the
no-fake-data rule exists to prevent. Measured before the fix: one emitting PID
and 0 `model.request.started`; after it, three PIDs and 11.

The fix routes the initializer through a function in *this* package, which the
child resolves to the real thing. These tests pin the two halves of that: the
substitution happens, and it is still picklable afterwards -- an initializer
that cannot be pickled would break the pool outright on spawn.
"""

import concurrent.futures.process as cfp
import functools
import os
import pickle

import pytest

from control_plane.telemetry import instrument


@pytest.fixture
def restore_pool_init():
    """Undo the class patch, so ordering between tests cannot matter."""
    original = cfp.ProcessPoolExecutor.__init__
    yield
    cfp.ProcessPoolExecutor.__init__ = original


def _captured_initializer(monkeypatch):
    """Install the hook and return whatever reaches the real constructor."""
    seen = {}

    def fake_init(self, max_workers=None, mp_context=None,
                  initializer=None, initargs=(), **kw):
        seen["initializer"] = initializer
        seen["initargs"] = initargs

    monkeypatch.setattr(cfp.ProcessPoolExecutor, "__init__", fake_init)
    instrument.install_pool_initializer_hook()
    cfp.ProcessPoolExecutor(max_workers=1, initializer=_upstream_initializer,
                            initargs=("cfg", "eval.py"))
    return seen


def _upstream_initializer(*args):
    """Stand-in for openevolve's `_worker_init`: module-level, so picklable."""


def test_the_initializer_is_replaced_when_telemetry_is_on(monkeypatch,
                                                          restore_pool_init):
    monkeypatch.setenv(instrument.ENV_ENABLED, "1")
    seen = _captured_initializer(monkeypatch)

    assert seen["initializer"] is not _upstream_initializer, (
        "the initializer reached the pool unwrapped, so a spawned worker would "
        "resolve upstream's function and install no telemetry")
    assert isinstance(seen["initializer"], functools.partial)
    assert seen["initializer"].func is instrument._worker_bootstrap
    # The original must still be invoked -- upstream's initializer is what sets
    # the worker's config and evaluation file, so dropping it breaks the run.
    assert seen["initializer"].args == (_upstream_initializer,)
    assert seen["initargs"] == ("cfg", "eval.py")


def test_the_replacement_survives_pickling(monkeypatch, restore_pool_init):
    """
    `spawn` pickles the initializer. If that fails the pool does not start at
    all, which would turn a silent gap into a loud outage.
    """
    monkeypatch.setenv(instrument.ENV_ENABLED, "1")
    seen = _captured_initializer(monkeypatch)

    revived = pickle.loads(pickle.dumps(seen["initializer"]))
    assert revived.func is instrument._worker_bootstrap
    assert revived.args == (_upstream_initializer,)


def test_the_initializer_is_untouched_when_telemetry_is_off(monkeypatch,
                                                            restore_pool_init):
    """
    The plain upstream CLI must behave exactly as upstream ships it, so the
    substitution is confined to runs that asked for telemetry.
    """
    monkeypatch.delenv(instrument.ENV_ENABLED, raising=False)
    seen = _captured_initializer(monkeypatch)

    assert seen["initializer"] is _upstream_initializer


def test_the_hook_is_idempotent(monkeypatch, restore_pool_init):
    """
    `install_worker_hook` runs in the parent and again in any child that
    re-installs. Wrapping the constructor twice would nest one partial inside
    another and call upstream's initializer twice.
    """
    monkeypatch.setenv(instrument.ENV_ENABLED, "1")
    instrument.install_pool_initializer_hook()
    once = cfp.ProcessPoolExecutor.__init__
    instrument.install_pool_initializer_hook()

    assert cfp.ProcessPoolExecutor.__init__ is once


def test_the_bootstrap_runs_the_original_after_installing(monkeypatch):
    """
    Order matters: telemetry is installed first so that anything the upstream
    initializer itself emits is captured.
    """
    calls = []
    monkeypatch.setattr(instrument, "auto_install_from_env",
                        lambda: calls.append("telemetry"))

    def original(*args):
        calls.append(("original",) + args)

    instrument._worker_bootstrap(original, "cfg", "eval.py")

    assert calls == ["telemetry", ("original", "cfg", "eval.py")]


def test_the_bootstrap_tolerates_no_original(monkeypatch):
    """A pool constructed without an initializer still gets telemetry."""
    calls = []
    monkeypatch.setattr(instrument, "auto_install_from_env",
                        lambda: calls.append("telemetry"))

    instrument._worker_bootstrap(None)

    assert calls == ["telemetry"]


def test_a_failing_telemetry_install_does_not_break_the_worker(monkeypatch):
    """
    Telemetry is observability. A worker that cannot report must still run the
    iteration rather than take the pool down with it.
    """
    def explode():
        raise RuntimeError("collector unreachable")

    monkeypatch.setattr(instrument, "auto_install_from_env", explode)
    ran = []

    instrument._worker_bootstrap(lambda *a: ran.append(a), "cfg")

    assert ran == [("cfg",)]


def test_spawn_is_the_default_context_somewhere_this_matters():
    """
    Not an assertion about this machine -- a note that the fork-only path is
    not universal. On Windows and macOS `spawn` is the default, which is where
    the unwrapped initializer costs every worker-side event.
    """
    import multiprocessing as mp

    assert mp.get_start_method(allow_none=False) in {"fork", "spawn", "forkserver"}
    if os.name == "nt":
        assert mp.get_start_method() == "spawn"
