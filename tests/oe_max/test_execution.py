"""
Running evolved code without trusting it.

An evolved program is code nobody wrote and nobody reviewed, produced by a
model rewarded for making a number go up. Upstream runs it in the evaluator's
own process.

These tests do not check that the limits are *configured*. They check that they
*stop things* — each one runs a program that actually tries to exceed a
ceiling, because a resource limit that is set and not enforced looks identical
to one that works right up until the moment it matters.

The container backend is exercised only where a runtime exists; where it does
not, the test asserts the honest-unavailability contract instead.
"""

import os

import pytest

from oe_max.execution import (
    ExecutionResult, ResourceLimits, SandboxedRunner, available_backends,
    describe_backends,
)
from oe_max.execution.runner import CPU, CRASHED, MEMORY, OK, TIMEOUT, UNAVAILABLE

pytestmark = pytest.mark.skipif(
    "subprocess" not in available_backends(),
    reason="POSIX resource limits unavailable on this platform")


def _runner(**kw):
    limits = ResourceLimits(cpu_seconds=kw.pop("cpu_seconds", 5),
                            memory_mb=kw.pop("memory_mb", 256),
                            file_size_mb=kw.pop("file_size_mb", 1),
                            processes=kw.pop("processes", 32),
                            wall_seconds=kw.pop("wall_seconds", 15.0),
                            env=kw.pop("env", {}))
    return SandboxedRunner(limits, backend=kw.pop("backend", "subprocess"))


# -- the happy path ---------------------------------------------------------

def test_a_well_behaved_program_runs():
    r = _runner().run_script("print('hello')")
    assert r.ok and r.exit_code == 0
    assert "hello" in r.stdout


def test_a_return_value_comes_back_separately_from_what_it_printed():
    """
    A candidate that prints is normal. Mixing its output with its answer makes
    the two indistinguishable.
    """
    r = _runner().run_code(
        "def solve():\n    print('working...')\n    return {'score': 0.75}\n",
        entry="solve")
    assert r.ok
    assert r.value == {"score": 0.75}
    assert "working..." in r.stdout


def test_an_unserialisable_return_value_comes_back_as_its_repr():
    r = _runner().run_code(
        "class Thing:\n    def __repr__(self):\n        return '<thing>'\n"
        "def solve():\n    return Thing()\n", entry="solve")
    assert r.ok and r.value == "<thing>"


def test_a_missing_entry_point_is_a_crash_not_a_silence():
    r = _runner().run_code("x = 1\n", entry="solve")
    assert r.status == CRASHED
    assert "solve" in (r.reason or "") + r.stderr


# -- the limits actually stop things ---------------------------------------

def test_a_runaway_loop_is_stopped_by_the_wall_clock():
    r = _runner(wall_seconds=2.0, cpu_seconds=60).run_script(
        "while True:\n    pass\n")
    assert r.status == TIMEOUT
    assert r.duration_s < 10, "the timeout did not actually fire"
    assert "wall clock" in (r.reason or "")


def test_cpu_time_is_capped_independently_of_wall_clock():
    """
    Distinguishable on purpose: "make it faster" and "make it smaller" are
    different instructions to give back to a search.
    """
    r = _runner(cpu_seconds=1, wall_seconds=30.0).run_script(
        "x = 0\nwhile True:\n    x += 1\n")
    assert r.status in (CPU, TIMEOUT)
    if r.status == CPU:
        assert "CPU time" in (r.reason or "")


def test_unbounded_allocation_is_stopped():
    r = _runner(memory_mb=128, wall_seconds=20.0).run_script(
        "chunks = []\n"
        "while True:\n"
        "    chunks.append(bytearray(16 * 1024 * 1024))\n")
    assert r.status in (MEMORY, CPU, TIMEOUT), r.to_dict()
    assert r.status != OK, "the memory ceiling did not stop an unbounded allocation"


def test_a_candidate_cannot_fill_the_disk():
    r = _runner(file_size_mb=1, wall_seconds=20.0).run_script(
        "with open('big.bin', 'wb') as fh:\n"
        "    for _ in range(200):\n"
        "        fh.write(b'x' * (1024 * 1024))\n")
    assert r.status != OK
    assert "File size limit" in r.stderr or "OSError" in r.stderr or r.status == MEMORY


def test_a_fork_bomb_hits_a_ceiling_rather_than_the_machine():
    r = _runner(processes=8, wall_seconds=10.0).run_script(
        "import subprocess, sys\n"
        "kids = []\n"
        "for _ in range(200):\n"
        "    kids.append(subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']))\n")
    assert r.status != OK


def test_a_timeout_kills_what_the_candidate_started():
    """
    Killing only the child leaves its workers consuming the machine long after
    the run that made them moved on. That is a leak, not a timeout.
    """
    marker = "oe_max_orphan_probe"
    r = _runner(wall_seconds=2.0).run_script(
        f"import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', "
        f"'import time; time.sleep(60)  # {marker}'])\n"
        f"time.sleep(60)\n")
    assert r.status == TIMEOUT

    import subprocess as sp
    survivors = sp.run(["ps", "-eo", "args"], capture_output=True, text=True)
    assert marker not in survivors.stdout.replace(f"# {marker}'])", ""), \
        "a process the candidate spawned outlived the timeout"


# -- what the candidate can see --------------------------------------------

def test_credentials_are_not_inherited():
    """
    The broker owns provider keys and no candidate has any reason to see one.
    An allowlist rather than a denylist, because a denylist that forgets a key
    leaks it to code a model wrote.
    """
    os.environ["OPENCODE_API_KEY"] = "sk-should-never-be-visible"
    try:
        r = _runner().run_script(
            "import os\nprint(sorted(k for k in os.environ if 'KEY' in k or 'TOKEN' in k))\n")
        assert r.ok
        assert "OPENCODE_API_KEY" not in r.stdout
    finally:
        os.environ.pop("OPENCODE_API_KEY", None)


def test_explicitly_passed_environment_does_reach_the_candidate():
    r = _runner(env={"TASK_MODE": "strict"}).run_script(
        "import os\nprint(os.environ.get('TASK_MODE'))\n")
    assert r.ok and "strict" in r.stdout


def test_each_execution_starts_from_an_empty_directory():
    """
    Sharing one directory makes a file written by one candidate appear to the
    next — cross-contamination that is very hard to see in the results.
    """
    runner = _runner()
    first = runner.run_script("open('left_behind.txt', 'w').write('x')\n")
    assert first.ok
    second = runner.run_script(
        "import os\nprint('left_behind.txt' in os.listdir('.'))\n")
    assert "False" in second.stdout


def test_hashing_is_deterministic_so_results_are_reproducible():
    """A candidate passing on set iteration order is a real source of flake."""
    script = "print(hash('evolution'))\n"
    assert _runner().run_script(script).stdout == _runner().run_script(script).stdout


# -- honest unavailability --------------------------------------------------

def test_naming_a_missing_backend_reports_it_rather_than_downgrading():
    """
    Silently falling back would tell a caller they had containment when they
    did not — the one failure mode worse than having no sandbox.
    """
    runner = SandboxedRunner(ResourceLimits(), backend="does-not-exist")
    r = runner.run_script("print(1)")
    assert r.status == UNAVAILABLE
    assert r.backend == "does-not-exist"


def test_every_unavailable_backend_states_a_reason():
    for backend in describe_backends():
        if not backend["available"]:
            assert backend["reason"], f"{backend['backend']} is unavailable with no reason"


def test_the_subprocess_backend_admits_what_it_does_not_stop():
    """Overstating isolation is worse than having none."""
    subproc = next(b for b in describe_backends() if b["backend"] == "subprocess")
    assert "network" in subproc["does_not_stop"]


def test_auto_prefers_the_stronger_backend():
    runner = SandboxedRunner(ResourceLimits(), backend="auto")
    expected = "container" if "container" in available_backends() else "subprocess"
    assert runner._resolve_backend() == expected


# -- failures are results, not exceptions -----------------------------------

def test_a_crashing_candidate_returns_a_result_with_its_traceback():
    r = _runner().run_script("raise ValueError('the candidate exploded')\n")
    assert r.status == CRASHED
    assert "the candidate exploded" in (r.reason or "") + r.stderr
    assert isinstance(r, ExecutionResult)


def test_a_syntax_error_is_a_result_too():
    r = _runner().run_script("def broken(:\n    pass\n")
    assert r.status == CRASHED and not r.ok


def test_output_is_bounded():
    """A program printing in a loop is a thing that happens."""
    r = _runner(wall_seconds=20.0).run_script(
        "for _ in range(200000):\n    print('x' * 100)\n")
    assert len(r.stdout) < 200_000


def test_the_import_gap_is_declared_rather_than_papered_over():
    """
    A candidate *can* `import oe_max`, because it runs on the interpreter this
    project is installed into and `-s` only skips the user site directory. The
    credentials are still out of reach — not in its environment, not in its
    process — but the code is not, and only the container backend with its own
    image closes that.

    The requirement is that the gap is declared. An operator who reads
    "isolated" and gets this would run something they otherwise would not.
    """
    r = _runner().run_script(
        "try:\n"
        "    import oe_max\n"
        "    print('IMPORTED')\n"
        "except ImportError:\n"
        "    print('BLOCKED')\n")
    assert r.ok
    subproc = next(b for b in describe_backends() if b["backend"] == "subprocess")
    if "IMPORTED" in r.stdout:
        assert "importing packages installed in the interpreter" in \
            subproc["does_not_stop"]


def test_credentials_are_still_out_of_reach_even_though_the_code_is():
    """The gap above is about code, not secrets."""
    os.environ["NVIDIA_API_KEY"] = "nvapi-must-not-leak"
    try:
        r = _runner().run_script(
            "import os\nprint(os.environ.get('NVIDIA_API_KEY', 'ABSENT'))\n")
        assert "ABSENT" in r.stdout
    finally:
        os.environ.pop("NVIDIA_API_KEY", None)


def test_an_installed_but_unusable_runtime_is_not_reported_as_available():
    """
    The CLI being on PATH proves nothing. On this machine `docker` exists and
    `docker info` fails — reporting the container backend as available on the
    strength of the binary would promise network isolation that every run would
    fail to deliver. Same mistake as trusting a provider's model listing
    without a smoke test.
    """
    from oe_max.execution.limits import (
        container_runtime, container_unavailable_reason, reset_runtime_cache,
    )

    reset_runtime_cache()
    try:
        unprobed = container_runtime(probe=False)
        probed = container_runtime()
        if unprobed and not probed:
            assert container_unavailable_reason()
            assert "not reachable" in container_unavailable_reason() or \
                "could not be run" in container_unavailable_reason()
        # Whatever the answer, availability and the reason must agree.
        entry = next(b for b in describe_backends() if b["backend"] == "container")
        assert bool(entry["available"]) == bool(probed)
        assert entry["available"] or entry["reason"]
    finally:
        reset_runtime_cache()
