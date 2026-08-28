"""
The native agent runtime, and how it reaches `OpenEvolve` without patching it.

These modules arrived from a session that placed them inside `openevolve/` and
bolted five methods onto `OpenEvolve` by editing `controller.py` directly. That
works, and it permanently ends the guarantee that makes an upstream merge a
fast-forward instead of a conflict resolution — 172 inserted lines across
`__init__.py`, `config.py` and `controller.py`, in the one tree this fork
promises never to touch (CLAUDE.md rule 1, enforced by
`tests/evolution/test_patch_surface.py`).

The code itself was fine. Its address was not: none of it needs to live there.
It only ever *reads* from the engine — `Program` and `ProgramDatabase` — which
an import does as well from here as from inside the package.

So the modules moved into this package and the five methods are attached at
runtime by `install()`, which is the pattern the fork already uses everywhere
else (`control_plane/telemetry/instrument.py` wraps public methods the same
way). The capability is identical; the patch surface stays empty.

    from control_plane.native import install
    install()                      # OpenEvolve now has the agent methods

Idempotent, and a no-op when the engine cannot be imported.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Optional, Sequence

logger = logging.getLogger(__name__)

_INSTALLED_FLAG = "__evolution_native_agent__"


def install() -> bool:
    """
    Attach the native-agent methods to `OpenEvolve`.

    Returns True when this call installed them, False when they were already
    present or the engine is unavailable — a caller that asked for the runtime
    must be able to tell it got one, rather than assume.
    """
    try:
        from openevolve.controller import OpenEvolve
    except Exception as exc:  # pragma: no cover - engine always present in-tree
        logger.debug("native agent install skipped: %r", exc)
        return False

    if getattr(OpenEvolve, _INSTALLED_FLAG, False):
        return False

    def _native_controller(self: Any):
        # Built lazily and cached on the instance: constructing it imports the
        # provider router and the agent world, which a plain `OpenEvolve()`
        # used for a non-agent run has no reason to pay for.
        existing = getattr(self, "_native_controller_instance", None)
        if existing is not None:
            return existing
        from pathlib import Path

        from .native_controller import NativeController

        # NativeController takes the run's output directory, not the engine:
        # every artefact it writes -- agent events, worlds, memory, goals,
        # sessions -- lives under it, so a native run is inspectable in exactly
        # the same place as an ordinary one.
        controller = NativeController(Path(self.output_dir))
        self._native_controller_instance = controller
        return controller

    def create_native_agent_runtime(self: Any, world_name: str = "candidate"):
        return _native_controller(self).create_runtime(world_name)

    def run_native_agent(
        self: Any,
        goal: Any,
        tasks: Sequence[Any],
        runner: Callable[[Any], str],
        evaluator: Callable[[Any], Mapping[str, float]],
        acceptance: Callable[[Mapping[str, float]], bool],
        world_name: str = "candidate",
        max_workers: int = 4,
    ):
        return _native_controller(self).run_agent(
            goal, tasks, runner, evaluator, acceptance, world_name, max_workers
        )

    def run_native_model_agent(
        self: Any,
        goal: Any,
        model_runtime: Optional[Any] = None,
        tools: Optional[Sequence[Any]] = None,
        context_items: Sequence[Any] = (),
        role: Optional[Any] = None,
        world_name: str = "candidate",
        max_steps: int = 12,
        max_tool_calls: int = 32,
        session_id: Optional[str] = None,
    ):
        from control_plane.providers.profiles import Role

        return _native_controller(self).run_model_agent(
            goal,
            model_runtime,
            tools,
            context_items,
            role=Role.ORCHESTRATOR if role is None else role,
            world_name=world_name,
            max_steps=max_steps,
            max_tool_calls=max_tool_calls,
            session_id=session_id,
        )

    def fork_native_model_session(self: Any, session_id: str):
        return _native_controller(self).fork_model_session(session_id)

    OpenEvolve._native_controller = _native_controller
    OpenEvolve.create_native_agent_runtime = create_native_agent_runtime
    OpenEvolve.run_native_agent = run_native_agent
    OpenEvolve.run_native_model_agent = run_native_model_agent
    OpenEvolve.fork_native_model_session = fork_native_model_session
    setattr(OpenEvolve, _INSTALLED_FLAG, True)
    return True


def installed() -> bool:
    """Whether the engine currently carries the native-agent methods."""
    try:
        from openevolve.controller import OpenEvolve
    except Exception:  # pragma: no cover
        return False
    return bool(getattr(OpenEvolve, _INSTALLED_FLAG, False))
