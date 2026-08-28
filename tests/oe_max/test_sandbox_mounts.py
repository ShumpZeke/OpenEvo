"""
A sandboxed script must be able to read the files it was told to read.

The container backend mounted only its throwaway workdir, while the script it
runs embeds *host* absolute paths for the evaluator and the candidate program.
Inside the container those paths do not exist, so a real evaluation died with
`[Errno 2] No such file or directory` pointing at the evaluator — a "crashed"
candidate that had never been run.

This went unseen for a simple reason: the sandbox tests skip without POSIX
resource limits, and the machine they were written on had no container runtime.
The container path first executed in CI. These tests deliberately exercise the
*argv construction* rather than a live container, so the wiring stays covered
on machines with no Docker — which is where it broke.
"""

import os

import pytest

from oe_max.execution.runner import _mount_roots


class TestMountRoots:
    def test_a_file_is_exposed_through_its_directory(self, tmp_path):
        """
        Mounting the file alone would break every task that is not a single
        module: an evaluator routinely imports a sibling, and upstream puts the
        evaluator's directory on `sys.path` before loading it.
        """
        target = tmp_path / "evaluator.py"
        target.write_text("x = 1")

        assert _mount_roots([str(target)]) == [str(tmp_path)]

    def test_a_directory_is_exposed_as_itself(self, tmp_path):
        assert _mount_roots([str(tmp_path)]) == [str(tmp_path)]

    def test_two_files_in_one_directory_collapse_to_one_mount(self, tmp_path):
        """
        The common case: evaluator and candidate live side by side. Passing the
        same target twice would make Docker reject the run.
        """
        for name in ("evaluator.py", "initial_program.py"):
            (tmp_path / name).write_text("x = 1")

        roots = _mount_roots([str(tmp_path / "evaluator.py"),
                              str(tmp_path / "initial_program.py")])

        assert roots == [str(tmp_path)]

    def test_a_nested_directory_collapses_into_its_ancestor(self, tmp_path):
        """
        Docker refuses a mount whose target sits inside another mount, so a
        nested pair has to become one.
        """
        child = tmp_path / "tasks" / "fn_min"
        child.mkdir(parents=True)
        (child / "evaluator.py").write_text("x = 1")

        roots = _mount_roots([str(tmp_path), str(child / "evaluator.py")])

        assert roots == [str(tmp_path)]

    def test_a_path_that_does_not_exist_is_dropped(self):
        """
        Better a script that fails on its own missing file than a container
        that refuses to start and reports nothing about the candidate.
        """
        assert _mount_roots([os.path.join(os.sep, "nope", "missing.py")]) == []

    @pytest.mark.parametrize("paths", [(), ("",), (None,)])
    def test_nothing_to_mount_is_not_an_error(self, paths):
        assert _mount_roots([p for p in paths if p is not None] or []) == []


class TestContainerArgv:
    """
    The mounts have to actually reach the command line, read-only, at the same
    absolute path they have on the host — a mount at a different path is no
    better than no mount, because the script's paths are baked in.
    """

    def _argv(self, monkeypatch, tmp_path, read_only_paths):
        from oe_max.execution import runner as runner_mod

        seen = {}

        class _Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            return _Completed()

        monkeypatch.setattr(runner_mod, "container_runtime", lambda: "docker")
        monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)

        r = runner_mod.SandboxedRunner(runner_mod.ResourceLimits(), backend="container")
        r._run_container(str(tmp_path), read_only_paths)
        return seen["argv"]

    def test_each_named_path_is_mounted_read_only_at_the_same_path(
            self, monkeypatch, tmp_path):
        task = tmp_path / "task"
        task.mkdir()
        (task / "evaluator.py").write_text("x = 1")

        argv = self._argv(monkeypatch, tmp_path / "work",
                          [str(task / "evaluator.py")])

        assert f"{task}:{task}:ro" in argv

    def test_the_isolation_flags_are_not_weakened_by_the_mounts(
            self, monkeypatch, tmp_path):
        """
        Exposing task files must not become a way in. The network stays off,
        the root filesystem stays read-only, capabilities stay dropped.
        """
        task = tmp_path / "task"
        task.mkdir()

        argv = self._argv(monkeypatch, tmp_path / "work", [str(task)])

        assert "--network" in argv and argv[argv.index("--network") + 1] == "none"
        assert "--read-only" in argv
        assert "--cap-drop" in argv and argv[argv.index("--cap-drop") + 1] == "ALL"
        # And the exposure is read-only, never writable.
        assert not any(a.endswith(f":{task}") for a in argv), \
            "a task path was mounted writable"

    def test_no_mounts_are_added_when_none_are_asked_for(
            self, monkeypatch, tmp_path):
        argv = self._argv(monkeypatch, tmp_path / "work", [])
        assert sum(1 for a in argv if a == "-v") == 1, \
            "only the workdir should be mounted"


class TestContainerImage:
    """
    `python:3.11-slim` carries no third-party packages, so a task whose
    evaluator imports numpy — as the shipped example does — cannot be imported
    inside it and every candidate is reported as crashed. That is a true
    statement about the image and a misleading one about the candidate.

    Installing at run time is not available: the container has no network by
    design. So the only workable answer is to point the sandbox at an image
    that already carries the task's dependencies, which makes the image a
    setting rather than a constant.
    """

    def test_the_default_image_is_used_when_nothing_is_configured(self, monkeypatch):
        from oe_max.execution import runner as runner_mod

        monkeypatch.delenv(runner_mod.ENV_IMAGE, raising=False)
        r = runner_mod.SandboxedRunner(runner_mod.ResourceLimits())

        assert r.image == runner_mod.DEFAULT_IMAGE

    def test_the_environment_overrides_the_default(self, monkeypatch):
        from oe_max.execution import runner as runner_mod

        monkeypatch.setenv(runner_mod.ENV_IMAGE, "registry.example/task:1.4")
        r = runner_mod.SandboxedRunner(runner_mod.ResourceLimits())

        assert r.image == "registry.example/task:1.4"

    def test_an_explicit_argument_beats_the_environment(self, monkeypatch):
        """
        A caller that names an image means it; the variable is the fallback for
        callers that do not.
        """
        from oe_max.execution import runner as runner_mod

        monkeypatch.setenv(runner_mod.ENV_IMAGE, "from-env:1")
        r = runner_mod.SandboxedRunner(runner_mod.ResourceLimits(),
                                       image="explicit:2")

        assert r.image == "explicit:2"

    def test_the_configured_image_is_what_the_container_runs(
            self, monkeypatch, tmp_path):
        """
        A setting that does not reach the command line is not a setting.
        """
        from oe_max.execution import runner as runner_mod

        seen = {}

        class _Completed:
            returncode = 0
            stdout = ""
            stderr = ""

        monkeypatch.setattr(runner_mod, "container_runtime", lambda: "docker")
        monkeypatch.setattr(runner_mod.subprocess, "run",
                            lambda argv, **kw: (seen.update(argv=argv), _Completed())[1])
        monkeypatch.setenv(runner_mod.ENV_IMAGE, "registry.example/task:1.4")

        r = runner_mod.SandboxedRunner(runner_mod.ResourceLimits(),
                                       backend="container")
        r._run_container(str(tmp_path), ())

        assert "registry.example/task:1.4" in seen["argv"]


class TestSandboxThreadPools:
    """
    A candidate's numeric libraries must not size themselves from the machine.

    OpenBLAS, MKL and OpenMP read the *host's* core count at import time and
    build a thread pool from it, ignoring what the process is actually allowed.
    Under the sandbox's RLIMIT_NPROC that is a hard failure, not a slow one: on
    a 16-core CI runner `import numpy` died with "OpenBLAS blas_thread_init:
    pthread_create failed", and the shipped example's evaluator was reported as
    a crashed candidate before its first line ran.

    Invisible on a machine where these tests skip for lack of POSIX resource
    limits, which is why it reached CI. The process ceiling is the security
    property and does not move, so the thread pools are what give way.
    """

    def test_the_numeric_libraries_are_pinned_to_one_thread(self):
        from oe_max.execution.limits import ResourceLimits

        env = ResourceLimits().child_env()

        for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                    "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
            assert env.get(var) == "1", var

    def test_a_caller_can_still_override_the_pin(self):
        """
        The pin is a default, not a ceiling: a task that genuinely wants more
        threads and has raised the process limit to match should get them.
        """
        from oe_max.execution.limits import ResourceLimits

        env = ResourceLimits(env={"OMP_NUM_THREADS": "4"}).child_env()

        assert env["OMP_NUM_THREADS"] == "4"
        # The others keep the safe default.
        assert env["OPENBLAS_NUM_THREADS"] == "1"

    def test_the_pin_does_not_leak_host_credentials(self):
        """
        The reason child_env exists at all: a candidate inherits an allowlist,
        never the parent's environment. Adding variables must not widen that.
        """
        import os

        from oe_max.execution.limits import ResourceLimits

        os.environ["NVIDIA_API_KEY"] = "nvapi-should-not-appear"
        try:
            env = ResourceLimits().child_env()
            assert "NVIDIA_API_KEY" not in env
            assert not any("nvapi-should-not-appear" == v for v in env.values())
        finally:
            os.environ.pop("NVIDIA_API_KEY", None)
