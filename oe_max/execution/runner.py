"""
Run a candidate in a separate process with real ceilings on it.

Design notes that are not incidental:

**A fresh working directory per execution, deleted afterwards.** Not because a
candidate cannot write elsewhere — it can, see `limits.py` — but because the
common case is a program that writes a file it expects to find later, and
sharing one directory between candidates makes that a source of cross-candidate
contamination that is very hard to see. Each candidate starts from nothing.

**The whole process group is killed on timeout, not just the child.** A
candidate that spawned workers leaves them running otherwise, and they keep
consuming the machine long after the run that made them has moved on. This is
the difference between a timeout and a leak.

**The environment is an allowlist, not the parent's environment minus a few
keys.** Provider credentials live in the parent process of anything the broker
started; a denylist that forgets one leaks it to code a model wrote.

**Failure is a result, not an exception.** A candidate that crashes, times out
or exhausts memory is ordinary output from evolutionary search, not an error
condition for the caller to handle. Callers get an `ExecutionResult` with a
classified failure and the output that was produced before it died.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .limits import ResourceLimits, container_runtime, describe_backends

# How much of a candidate's output to keep. Enough to see a traceback; bounded,
# because a program printing in a loop is a thing that happens.
MAX_OUTPUT_BYTES = 64 * 1024

# The container image candidates run in. Deliberately minimal, and deliberately
# overridable: a task's own dependencies have to be present in the image or its
# evaluator cannot even be imported.
DEFAULT_IMAGE = "python:3.11-slim"
ENV_IMAGE = "OE_MAX_SANDBOX_IMAGE"

# Failure classes, kept coarse on purpose: the caller acts on the class, and
# the detail is in stderr.
OK = "ok"
TIMEOUT = "timeout"
MEMORY = "memory_exhausted"
CPU = "cpu_exhausted"
CRASHED = "crashed"
UNAVAILABLE = "backend_unavailable"


@dataclass
class ExecutionResult:
    status: str
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    backend: str = "subprocess"
    value: Any = None            # parsed from the result file, when written
    limits: Dict[str, Any] = field(default_factory=dict)
    reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.status == OK

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "exit_code": self.exit_code,
                "duration_s": round(self.duration_s, 3), "backend": self.backend,
                "stdout": self.stdout, "stderr": self.stderr,
                "value": self.value, "limits": self.limits, "reason": self.reason}


def available_backends() -> List[str]:
    return [b["backend"] for b in describe_backends() if b["available"]]


def _mount_roots(paths: Sequence[str]) -> List[str]:
    """
    The directories to expose, deduplicated and with nested paths collapsed.

    A file is exposed via its directory: an evaluator almost always imports a
    sibling, and upstream puts the evaluator's directory on `sys.path` before
    loading it, so mounting the file alone would break exactly the tasks that
    are not single-file. Nested entries collapse into their ancestor because
    Docker rejects a mount whose target sits inside another mount.
    """
    roots: List[str] = []
    for raw in paths:
        if not raw:
            continue
        full = os.path.abspath(raw)
        root = full if os.path.isdir(full) else os.path.dirname(full)
        if not root or not os.path.exists(root):
            continue
        roots.append(root)

    kept: List[str] = []
    for root in sorted(set(roots), key=len):
        if any(root == k or root.startswith(k + os.sep) for k in kept):
            continue
        kept.append(root)
    return kept


class SandboxedRunner:
    """
    Executes candidate code with resource ceilings.

    `backend="auto"` picks the strongest available. Naming a backend explicitly
    and not getting it is reported as `backend_unavailable` with the reason —
    silently falling back would tell a caller they had containment when they
    did not.
    """

    def __init__(self, limits: Optional[ResourceLimits] = None,
                 backend: str = "auto", image: Optional[str] = None) -> None:
        self.limits = limits or ResourceLimits()
        self.backend = backend
        # `python:3.11-slim` carries no third-party packages, so a task whose
        # evaluator imports numpy -- as the shipped example does -- cannot run
        # in it. That is inherent to running arbitrary tasks in a container
        # rather than a defect, which is exactly why the image has to be
        # settable: point OE_MAX_SANDBOX_IMAGE at one that has the task's
        # dependencies. See SANDBOX.md.
        self.image = image or os.environ.get(ENV_IMAGE) or DEFAULT_IMAGE

    # -- entry points --------------------------------------------------

    def run_code(self, code: str, *, entry: Optional[str] = None) -> ExecutionResult:
        """
        Execute a program, optionally calling one function and capturing what
        it returned.

        The return value comes back through a file rather than stdout, because
        a candidate that prints is normal and mixing the two makes a program's
        own output indistinguishable from its answer.
        """
        script = _RESULT_HARNESS.format(entry=json.dumps(entry)) if entry else ""
        return self.run_script(code + ("\n\n" + script if script else ""))

    def run_script(self, script: str,
                   read_only_paths: Optional[Sequence[str]] = None) -> ExecutionResult:
        """
        Run `script` under the configured ceilings.

        `read_only_paths` names host paths the script must be able to read --
        an evaluator module and the program under test, typically. The
        subprocess backend already shares the host filesystem and ignores it;
        the container backend bind-mounts each one read-only at the same
        absolute path, so a script written with host paths works unchanged in
        both. Anything not named here stays invisible to the candidate.
        """
        backend = self._resolve_backend()
        if backend is None:
            wanted = self.backend
            reason = next((b["reason"] for b in describe_backends()
                           if b["backend"] == wanted), "no backend is available")
            return ExecutionResult(UNAVAILABLE, backend=wanted, reason=reason,
                                   limits=self.limits.to_dict())

        workdir = tempfile.mkdtemp(prefix="oe-max-exec-")
        try:
            path = os.path.join(workdir, "candidate.py")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(script)
            # mkdtemp gives 0700 and the file 0600, owned by the host user. A
            # container whose image does not run as root -- or a daemon with
            # user-namespace remapping, which is what GitHub's runners use --
            # then cannot read the very script it was asked to run, and the
            # candidate "crashes" with Errno 13 rather than failing honestly.
            # Widening is safe here: this directory is a throwaway holding one
            # candidate program, and the sandbox is given no credentials.
            os.chmod(workdir, 0o755)
            os.chmod(path, 0o644)
            if backend == "container":
                return self._run_container(workdir, read_only_paths or ())
            return self._run_subprocess(workdir)
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # -- backends ------------------------------------------------------

    def _resolve_backend(self) -> Optional[str]:
        available = available_backends()
        if self.backend == "auto":
            # Strongest first: containment that exists beats containment that
            # is merely configured.
            for candidate in ("container", "subprocess"):
                if candidate in available:
                    return candidate
            return None
        return self.backend if self.backend in available else None

    def _run_subprocess(self, workdir: str) -> ExecutionResult:
        limits = self.limits

        def preexec() -> None:
            # Own process group, so a timeout can kill everything the candidate
            # started rather than only the candidate.
            os.setsid()
            import resource

            for limit, values in limits.as_rlimits():
                try:
                    resource.setrlimit(limit, values)
                except (ValueError, OSError):
                    # A limit this platform will not accept is skipped rather
                    # than fatal: partial containment beats none, and the gap
                    # is already documented per backend.
                    pass

        started = time.perf_counter()
        try:
            proc = subprocess.Popen(
                # `-s -B`, not `-I`. `-I` implies `-E`, which makes the
                # interpreter ignore every PYTHON* variable — including
                # PYTHONHASHSEED, so hashing goes back to being randomised per
                # process and a candidate can pass or fail on set iteration
                # order. The isolation `-E` would have given is already
                # provided, and more precisely, by the environment allowlist.
                [sys.executable, "-s", "-B", "candidate.py"],
                cwd=workdir, env=limits.child_env(), preexec_fn=preexec,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
        except OSError as exc:
            return ExecutionResult(CRASHED, stderr=str(exc),
                                   limits=limits.to_dict())

        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=limits.wall_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
            stdout, stderr = proc.communicate()

        duration = time.perf_counter() - started
        return self._classify(proc.returncode, stdout, stderr, duration,
                              workdir, timed_out, "subprocess")

    def _run_container(self, workdir: str,
                       read_only_paths: Sequence[str] = ()) -> ExecutionResult:
        runtime = container_runtime()
        limits = self.limits
        argv = [
            runtime, "run", "--rm",
            "--network", "none",                 # the thing subprocess cannot do
            "--read-only",
            "--tmpfs", "/tmp:rw,size=64m",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--memory", f"{limits.memory_mb}m",
            "--pids-limit", str(limits.processes),
            "--cpus", "1",
            "-v", f"{workdir}:/work",
            "-w", "/work",
        ]
        # Each named host path, read-only, at the same absolute path it has on
        # the host -- the script embeds host paths, so the mount point has to
        # match or it may as well not be mounted. Directories are mounted whole
        # because an evaluator is routinely one module among several in its own
        # directory, and upstream puts that directory on sys.path.
        for host_path in _mount_roots(read_only_paths):
            argv += ["-v", f"{host_path}:{host_path}:ro"]
        for key, value in limits.env.items():
            argv += ["-e", f"{key}={value}"]
        argv += [self.image, "python", "-s", "-B", "candidate.py"]

        started = time.perf_counter()
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True,
                timeout=limits.wall_seconds)
        except subprocess.TimeoutExpired as exc:
            return ExecutionResult(
                TIMEOUT, backend="container", duration_s=limits.wall_seconds,
                stdout=_clip(exc.stdout), stderr=_clip(exc.stderr),
                limits=limits.to_dict(),
                reason=f"exceeded {limits.wall_seconds}s wall clock")
        except OSError as exc:
            return ExecutionResult(CRASHED, backend="container", stderr=str(exc),
                                   limits=limits.to_dict())

        duration = time.perf_counter() - started
        return self._classify(proc.returncode, proc.stdout, proc.stderr,
                              duration, workdir, False, "container")

    # -- classification ------------------------------------------------

    def _classify(self, code: Optional[int], stdout: str, stderr: str,
                  duration: float, workdir: str, timed_out: bool,
                  backend: str) -> ExecutionResult:
        result = ExecutionResult(
            OK, exit_code=code, stdout=_clip(stdout), stderr=_clip(stderr),
            duration_s=duration, backend=backend, limits=self.limits.to_dict())

        if timed_out:
            result.status = TIMEOUT
            result.reason = f"exceeded {self.limits.wall_seconds}s wall clock"
            return result

        if code == 0:
            result.value = _read_value(workdir)
            return result

        # A negative code is a signal. SIGXCPU and SIGKILL are what the CPU and
        # memory ceilings actually produce, and telling them apart is the
        # difference between "make it faster" and "make it smaller".
        if code is not None and code < 0:
            signum = -code
            if signum == getattr(signal, "SIGXCPU", None):
                result.status, result.reason = CPU, (
                    f"exceeded {self.limits.cpu_seconds}s of CPU time")
                return result
            if signum in (signal.SIGKILL, signal.SIGSEGV):
                result.status, result.reason = MEMORY, (
                    f"killed by signal {signum}; usually the "
                    f"{self.limits.memory_mb}MB address-space ceiling")
                return result

        # An allocation refused by RLIMIT_AS — rather than the process being
        # killed outright — surfaces as an ordinary traceback. numpy phrases it
        # differently from CPython, and both are unambiguous.
        for marker in ("MemoryError", "Unable to allocate", "Cannot allocate memory"):
            if marker in stderr:
                result.status, result.reason = MEMORY, marker
                return result

        # Deliberately not reinterpreted: an allocation refused *inside* a C
        # extension can surface as "SystemError: error return without exception
        # set", which is genuinely ambiguous. Guessing "memory" from it would
        # mislabel unrelated extension bugs, so it stays a crash with the real
        # message attached.

        result.status = CRASHED
        result.reason = _first_exception(stderr) or f"exit code {code}"
        return result


# The harness appended when a caller wants a function's return value. Writing
# to a file keeps it separate from anything the candidate itself prints.
_RESULT_HARNESS = '''
if True:  # oe-max result harness
    import json as _json
    _entry = {entry}
    _fn = globals().get(_entry)
    if _fn is None:
        raise SystemExit(f"entry point {{_entry!r}} is not defined")
    _out = _fn()
    try:
        _payload = _json.dumps(_out)
    except (TypeError, ValueError):
        _payload = _json.dumps(repr(_out))
    with open("oe_max_result.json", "w", encoding="utf-8") as _fh:
        _fh.write(_payload)
'''


def _read_value(workdir: str) -> Any:
    path = os.path.join(workdir, "oe_max_result.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the whole group; a candidate's children are the point."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _clip(text: Optional[str]) -> str:
    if not text:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    if len(text) <= MAX_OUTPUT_BYTES:
        return text
    return text[:MAX_OUTPUT_BYTES] + f"\n… «truncated {len(text) - MAX_OUTPUT_BYTES} chars»"


def _first_exception(stderr: str) -> Optional[str]:
    """The last line of a traceback, which is the part that says what happened."""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    return lines[-1] if lines else None
