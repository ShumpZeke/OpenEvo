"""
OpenCode isolation.

The operator uses OpenCode for unrelated work. Evolution must never touch that.
This module is the enforcement point for that boundary (SOURCE_OF_TRUTH,
"OPENCode NON-INTERFERENCE BOUNDARY").

The rule we implement is narrow and absolute: Evolution may *read* a globally
installed OpenCode binary and may *execute* it, but every byte of configuration,
state, cache, session and log it produces is redirected into
`<workspace>/.evolution/opencode/`. Nothing outside that tree is written.

Redirection is by environment, not by mutating any file the operator owns:

    HOME / USERPROFILE     → Evolution's private home
    XDG_CONFIG_HOME        → .evolution/opencode/config
    XDG_DATA_HOME          → .evolution/opencode/state
    XDG_CACHE_HOME         → .evolution/opencode/cache
    XDG_STATE_HOME         → .evolution/opencode/state

A child process started with this environment cannot find the operator's global
config, so it cannot read or overwrite it even if it tried. That is a stronger
guarantee than a policy that merely promises not to write there.

`preflight()` reports honestly whether isolation is achievable. If it is not,
the caller must disable the OpenCode backend and keep native OpenEvolve
evaluators working, rather than proceeding and risking the operator's setup.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class IsolationLevel(str, Enum):
    CONTAINER = "container"          # strongest: nothing of the host is visible
    PROJECT_LOCAL = "project_local"  # dedicated HOME/XDG with a shared binary
    UNAVAILABLE = "unavailable"      # cannot isolate → backend must be disabled


# Paths we will never write to, whatever configuration asks for. Checked
# defensively before any write so a bad config value cannot escape the tree.
def _forbidden_roots() -> List[str]:
    home = os.path.expanduser("~")
    return [
        os.path.join(home, ".config", "opencode"),
        os.path.join(home, ".local", "share", "opencode"),
        os.path.join(home, ".cache", "opencode"),
        os.path.join(home, ".opencode"),
        os.path.join(home, ".config", "omo"),
        os.path.join(home, ".oh-my-openagent"),
        os.path.join(home, ".config", "oh-my-openagent"),
    ]


@dataclass
class IsolationReport:
    level: IsolationLevel
    ok: bool
    root: str
    binary: Optional[str] = None
    binary_version: Optional[str] = None
    binary_source: str = ""       # "global" | "project-local" | "container"
    owned_paths: Dict[str, str] = field(default_factory=dict)
    env_overrides: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    docker_available: bool = False
    omo: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level.value,
            "ok": self.ok,
            "root": self.root,
            "binary": self.binary,
            "binary_version": self.binary_version,
            "binary_source": self.binary_source,
            "owned_paths": self.owned_paths,
            # Values are directory paths, not secrets, so echoing them is safe
            # and lets the System page show exactly what Evolution owns.
            "env_overrides": self.env_overrides,
            "warnings": self.warnings,
            "reasons": self.reasons,
            "docker_available": self.docker_available,
            "omo": self.omo,
            "never_touched": _forbidden_roots(),
        }


class OpenCodeIsolation:
    """Creates and validates Evolution's private OpenCode environment."""

    def __init__(self, workspace: str) -> None:
        self.workspace = os.path.abspath(workspace)
        self.root = os.path.join(self.workspace, ".evolution", "opencode")
        self.omo_root = os.path.join(self.workspace, ".evolution", "omo")
        self.sandbox_root = os.path.join(self.workspace, ".evolution", "sandboxes")

    # -- layout --------------------------------------------------------

    def paths(self) -> Dict[str, str]:
        return {
            "root": self.root,
            "home": os.path.join(self.root, "home"),
            "config": os.path.join(self.root, "config"),
            "state": os.path.join(self.root, "state"),
            "cache": os.path.join(self.root, "cache"),
            "sessions": os.path.join(self.root, "sessions"),
            "logs": os.path.join(self.root, "logs"),
            "omo_config": os.path.join(self.omo_root, "config"),
            "omo_state": os.path.join(self.omo_root, "state"),
            "omo_logs": os.path.join(self.omo_root, "logs"),
            "sandboxes": self.sandbox_root,
        }

    def ensure_layout(self) -> Dict[str, str]:
        paths = self.paths()
        for key, path in paths.items():
            self._assert_safe(path)
            os.makedirs(path, exist_ok=True)
        return paths

    @staticmethod
    def _assert_safe(path: str) -> None:
        """Refuse to create or write anything under an operator-owned root."""
        target = os.path.abspath(path)
        for forbidden in _forbidden_roots():
            f = os.path.abspath(forbidden)
            if target == f or target.startswith(f + os.sep):
                raise PermissionError(
                    f"refusing to write inside the operator's OpenCode/OMO state: {target}"
                )

    # -- environment ---------------------------------------------------

    def env(self, extra: Optional[Dict[str, str]] = None,
            candidate_home: Optional[str] = None) -> Dict[str, str]:
        """
        Build the environment for an isolated OpenCode process.

        Starts from a *filtered* copy of the parent environment: variables that
        would point the child back at the operator's installation are dropped
        rather than overwritten, so a stale value cannot leak through.
        """
        paths = self.ensure_layout()
        home = candidate_home or paths["home"]
        self._assert_safe(home)
        os.makedirs(home, exist_ok=True)

        env = {
            k: v for k, v in os.environ.items()
            if k not in {
                "HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                "XDG_CACHE_HOME", "XDG_STATE_HOME",
                "OPENCODE_CONFIG", "OPENCODE_CONFIG_DIR", "OPENCODE_DATA_DIR",
                "OPENCODE_CACHE_DIR", "OPENCODE_STATE_DIR", "OPENCODE_HOME",
                "OPENCODE_SESSION_DIR", "OPENCODE_LOG_DIR",
                "OMO_CONFIG_DIR", "OMO_STATE_DIR", "OMO_HOME",
            }
        }
        overrides = {
            "HOME": home,
            "USERPROFILE": home,
            "XDG_CONFIG_HOME": paths["config"],
            "XDG_DATA_HOME": paths["state"],
            "XDG_CACHE_HOME": paths["cache"],
            "XDG_STATE_HOME": paths["state"],
            # Tool-specific overrides too: if OpenCode honours any of these it
            # lands in our tree; if it honours none, HOME/XDG still contain it.
            "OPENCODE_CONFIG_DIR": paths["config"],
            "OPENCODE_DATA_DIR": paths["state"],
            "OPENCODE_CACHE_DIR": paths["cache"],
            "OPENCODE_STATE_DIR": paths["state"],
            "OPENCODE_SESSION_DIR": paths["sessions"],
            "OPENCODE_LOG_DIR": paths["logs"],
            "OMO_CONFIG_DIR": paths["omo_config"],
            "OMO_STATE_DIR": paths["omo_state"],
            "EVOLUTION_ISOLATED": "1",
        }
        env.update(overrides)
        env.update(extra or {})
        return env

    def env_overrides(self) -> Dict[str, str]:
        base = {k: v for k, v in self.env().items()}
        keys = [
            "HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
            "XDG_STATE_HOME", "OPENCODE_CONFIG_DIR", "OPENCODE_DATA_DIR",
            "OPENCODE_CACHE_DIR", "OPENCODE_SESSION_DIR", "OPENCODE_LOG_DIR",
            "OMO_CONFIG_DIR", "OMO_STATE_DIR",
        ]
        return {k: base[k] for k in keys if k in base}

    # -- discovery -----------------------------------------------------

    def find_binary(self) -> Optional[str]:
        """
        Locate an OpenCode binary.

        A project-local install wins over a global one. A global binary is used
        read-only — we execute it, we never reconfigure or upgrade it.
        """
        local = os.path.join(self.root, "bin", "opencode")
        for candidate in (local, local + ".exe"):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return shutil.which("opencode")

    def binary_version(self, binary: str) -> Optional[str]:
        try:
            proc = subprocess.run(
                [binary, "--version"], capture_output=True, text=True, timeout=20,
                env=self.env(),
            )
            out = (proc.stdout or proc.stderr or "").strip()
            return out.splitlines()[0] if out else None
        except Exception:
            return None

    def detect_omo(self) -> Dict[str, Any]:
        """
        Detect Oh My OpenAgent without assuming a package name.

        OMO has gone through naming transitions, so we probe several plausible
        commands and report what we actually find. Nothing here is hardcoded as
        "the" install command, and absence is a normal, non-fatal outcome:
        OpenCode-only and native modes must keep working (section 5.4).
        """
        result: Dict[str, Any] = {
            "available": False, "binary": None, "version": None,
            "checked": [], "note": (
                "Oh My OpenAgent is optional and fast-moving; the install path is "
                "resolved at runtime rather than hardcoded. Absence does not "
                "affect OpenCode-only or native OpenEvolve evaluation."
            ),
        }
        for name in ("omo", "oh-my-openagent", "ohmyopenagent"):
            path = shutil.which(name)
            result["checked"].append({"command": name, "found": bool(path)})
            if path and not result["available"]:
                result["available"] = True
                result["binary"] = path
                try:
                    proc = subprocess.run([path, "--version"], capture_output=True,
                                          text=True, timeout=20, env=self.env())
                    result["version"] = (proc.stdout or proc.stderr or "").strip() or None
                except Exception as exc:
                    result["version_error"] = str(exc)
        return result

    @staticmethod
    def docker_available() -> bool:
        docker = shutil.which("docker")
        if not docker:
            return False
        try:
            return subprocess.run([docker, "info"], capture_output=True,
                                  timeout=25).returncode == 0
        except Exception:
            return False

    # -- preflight -----------------------------------------------------

    def preflight(self) -> IsolationReport:
        """Decide whether the OpenCode backend may be enabled at all."""
        try:
            paths = self.ensure_layout()
        except PermissionError as exc:
            return IsolationReport(
                level=IsolationLevel.UNAVAILABLE, ok=False, root=self.root,
                reasons=[str(exc)],
                warnings=["OpenCode sandbox backend must stay disabled; "
                          "native OpenEvolve evaluators are unaffected."],
            )

        binary = self.find_binary()
        docker_ok = self.docker_available()
        report = IsolationReport(
            level=IsolationLevel.UNAVAILABLE, ok=False, root=self.root,
            binary=binary, owned_paths=paths, env_overrides=self.env_overrides(),
            docker_available=docker_ok, omo=self.detect_omo(),
        )

        if binary:
            report.binary_version = self.binary_version(binary)
            report.binary_source = (
                "project-local" if binary.startswith(self.root) else "global"
            )
            if report.binary_source == "global":
                report.warnings.append(
                    "Using the globally installed OpenCode binary as an executable "
                    "only. Its configuration, state, cache and sessions are NOT "
                    "read or modified — Evolution redirects HOME and every XDG "
                    "path into its own tree."
                )

        if docker_ok and binary:
            report.level = IsolationLevel.CONTAINER
            report.ok = True
        elif binary:
            report.level = IsolationLevel.PROJECT_LOCAL
            report.ok = True
            report.warnings.append(
                "No container runtime detected, so candidates run in a project-local "
                "worktree with a dedicated HOME rather than a disposable container. "
                "Filesystem isolation is weaker than a container's."
            )
        else:
            report.reasons.append(
                "No OpenCode binary found (looked for a project-local install, then PATH)."
            )
            report.warnings.append(
                "OpenCode sandbox backend disabled. Native OpenEvolve evaluation "
                "continues to work; this is the documented fallback, not a failure."
            )

        # Final guard: assert nothing we own overlaps operator-owned state.
        for key, path in paths.items():
            try:
                self._assert_safe(path)
            except PermissionError as exc:
                report.ok = False
                report.level = IsolationLevel.UNAVAILABLE
                report.reasons.append(f"{key}: {exc}")
        return report

    def status(self) -> Dict[str, Any]:
        """Shape rendered by the System Health page."""
        try:
            report = self.preflight()
            data = report.to_dict()
        except Exception as exc:
            data = {
                "level": IsolationLevel.UNAVAILABLE.value, "ok": False,
                "root": self.root, "reasons": [f"preflight failed: {exc}"],
                "never_touched": _forbidden_roots(),
            }
        data["checked_at"] = time.time()
        return data

    def write_project_config(self, config: Dict[str, Any]) -> str:
        """
        Write Evolution's own OpenCode config inside the isolated tree.

        Never merges with, reads from, or migrates the operator's config.
        """
        paths = self.ensure_layout()
        target = os.path.join(paths["config"], "opencode", "opencode.json")
        self._assert_safe(target)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(config, fh, indent=2)
        return target
