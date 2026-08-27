"""
What each backend actually stops — stated precisely, including the gaps.

Overstating isolation is worse than having none, because an operator who
believes candidates are contained will run something they otherwise would not.
Every claim below is a claim about a mechanism that exists in this code.

subprocess backend
------------------

  stopped   CPU time            RLIMIT_CPU, SIGXCPU then SIGKILL
            address space       RLIMIT_AS
            file size           RLIMIT_FSIZE — a candidate cannot fill the disk
            process count       RLIMIT_NPROC — a fork bomb hits a ceiling
            core dumps          RLIMIT_CORE = 0
            wall-clock          timeout, then the whole process group is killed
            working directory   a fresh temp dir per run, deleted afterwards
            environment         cleared except an explicit allowlist

  NOT stopped
            network             the process can open sockets. There is no
                                portable way to prevent that from inside the
                                process being confined; it needs namespaces or
                                a container, which is what the container
                                backend is for.
            reading the disk    RLIMIT_FSIZE bounds writes, not reads. A
                                candidate can read anything the user can.
            escaping the cwd    it is a working directory, not a root. An
                                absolute path still resolves.
            imports             the candidate runs on the same interpreter the
                                project is installed into, so it can `import
                                oe_max`. Credentials are still not reachable
                                (they are neither in its environment nor in
                                its process) but the code is. Only the
                                container backend, with its own image, closes
                                this.

  So: it contains accidents, not attacks.

container backend
-----------------

  Adds network isolation (`--network none`), a read-only root filesystem, a
  dropped capability set and a real filesystem boundary. Requires docker or
  podman on PATH; when neither is present the backend reports itself
  unavailable *with the reason* rather than silently degrading to subprocess —
  a caller that asked for containment must be told it did not get it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Deliberately small. An evaluated candidate that needs more than this is
# either doing something the task did not ask for, or the caller should raise
# the limit knowingly rather than discover there was never one.
DEFAULT_CPU_SECONDS = 30
DEFAULT_MEMORY_MB = 1024
DEFAULT_FILE_SIZE_MB = 64
DEFAULT_PROCESSES = 64
DEFAULT_WALL_SECONDS = 60.0

# The only environment variables a candidate inherits. Everything else is
# dropped — most importantly every provider credential, which the broker owns
# and no candidate has any reason to see.
# PYTHONPATH is deliberately absent: inheriting the parent's would let a
# candidate import this project's own modules — including the broker that holds
# the credentials — which is precisely what a sandbox is for.
ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "PYTHONHASHSEED",
                 "PYTHONDONTWRITEBYTECODE")


@dataclass
class ResourceLimits:
    """Per-execution ceilings. Every field maps to a mechanism, not a wish."""

    cpu_seconds: int = DEFAULT_CPU_SECONDS
    memory_mb: int = DEFAULT_MEMORY_MB
    file_size_mb: int = DEFAULT_FILE_SIZE_MB
    processes: int = DEFAULT_PROCESSES
    wall_seconds: float = DEFAULT_WALL_SECONDS
    # Extra environment a caller explicitly wants the candidate to see. Never
    # populated from the parent process: credentials leak that way.
    env: Dict[str, str] = field(default_factory=dict)

    def as_rlimits(self) -> List[Any]:
        """(resource, (soft, hard)) pairs, skipping any this platform lacks."""
        import resource

        pairs = [
            ("RLIMIT_CPU", (self.cpu_seconds, self.cpu_seconds + 1)),
            ("RLIMIT_AS", (self.memory_mb * 1024 * 1024,) * 2),
            ("RLIMIT_FSIZE", (self.file_size_mb * 1024 * 1024,) * 2),
            ("RLIMIT_NPROC", (self.processes, self.processes)),
            ("RLIMIT_CORE", (0, 0)),
        ]
        out = []
        for name, values in pairs:
            limit = getattr(resource, name, None)
            if limit is not None:
                out.append((limit, values))
        return out

    def child_env(self) -> Dict[str, str]:
        """The environment the candidate runs with: allowlisted, then extras."""
        env = {k: os.environ[k] for k in ENV_ALLOWLIST if k in os.environ}
        env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        # Deterministic hashing, so a candidate cannot pass or fail on set
        # iteration order — a real source of "it worked when I ran it".
        env.setdefault("PYTHONHASHSEED", "0")
        env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
        env.update(self.env)
        return env

    def to_dict(self) -> Dict[str, Any]:
        return {"cpu_seconds": self.cpu_seconds, "memory_mb": self.memory_mb,
                "file_size_mb": self.file_size_mb, "processes": self.processes,
                "wall_seconds": self.wall_seconds}


# Probing the daemon costs a subprocess, and `describe_backends()` is called
# from status endpoints. Cached per process; `reset_runtime_cache()` clears it.
_RUNTIME_CACHE: Dict[str, Any] = {}
_PROBE_TIMEOUT_S = 5.0


def container_runtime(*, probe: bool = True) -> Optional[str]:
    """
    A container runtime that actually works, not merely one that is installed.

    The CLI being on PATH proves nothing: on this machine `docker` exists and
    `docker info` fails with "cannot connect to the Docker daemon". Reporting
    the container backend as available on the strength of the binary would tell
    an operator they had network isolation when every run would fail — the same
    mistake as trusting a provider's model listing without a smoke test.
    """
    if "runtime" in _RUNTIME_CACHE:
        return _RUNTIME_CACHE["runtime"]

    found = None
    reason = "neither docker nor podman is on PATH"
    for binary in ("docker", "podman"):
        if not shutil.which(binary):
            continue
        if not probe:
            found = binary
            break
        try:
            proc = subprocess.run([binary, "info"], capture_output=True,
                                  timeout=_PROBE_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError):
            reason = f"{binary} is installed but could not be run"
            continue
        if proc.returncode == 0:
            found = binary
            break
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        reason = (f"{binary} is installed but its daemon is not reachable"
                  + (f": {detail[0][:120]}" if detail else ""))

    if probe:
        _RUNTIME_CACHE["runtime"] = found
        _RUNTIME_CACHE["reason"] = None if found else reason
    return found


def container_unavailable_reason() -> Optional[str]:
    container_runtime()
    return _RUNTIME_CACHE.get("reason")


def reset_runtime_cache() -> None:
    _RUNTIME_CACHE.clear()


def describe_backends() -> List[Dict[str, Any]]:
    """
    What is available, and why anything unavailable is not.

    The reason is the point: an unsupported control rendered without one is
    indistinguishable from a broken control, and the operator cannot tell
    whether to install something or file a bug.
    """
    try:
        import resource  # noqa: F401  (import proves the module exists on this platform)
    except ImportError:
        pass

    have_rlimits = hasattr(os, "setsid") and os.name == "posix"
    runtime = container_runtime()
    return [
        {
            "backend": "subprocess",
            "available": have_rlimits,
            "reason": None if have_rlimits else
                      "POSIX resource limits and process groups are unavailable "
                      "on this platform",
            "stops": ["cpu", "memory", "file size", "process count", "wall clock"],
            "does_not_stop": ["network", "reading the filesystem",
                              "importing packages installed in the interpreter"],
        },
        {
            "backend": "container",
            "available": bool(runtime),
            "reason": None if runtime else container_unavailable_reason(),
            "runtime": runtime,
            "stops": ["cpu", "memory", "file size", "process count",
                      "wall clock", "network", "filesystem"],
            "does_not_stop": [],
        },
    ]
