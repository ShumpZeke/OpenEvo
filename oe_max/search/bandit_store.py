"""
Bandit state that survives the process boundary.

The bandit has been built and tested since the search layer was written, and
nothing uses it. Operator selection is uniform random, and the reason is not
that nobody got round to it — it is that the two halves of a bandit live in
different processes.

    selection   happens in a WORKER, inside `PromptSampler.build_prompt`
    reward      is known in the MAIN process, after evaluation, in
                `ProgramDatabase.add`

docs/gotchas.md covers the worker→main direction: `Program.metadata` is the one
channel that crosses, which is how a candidate's operator gets home. This is
the *other* direction, main→worker, and there is no in-memory channel for it at
all. A ContextVar, a global, a module-level selector — each is correct inside
one process and simply does not exist in the other, which is exactly the class
of bug that cost a debugging cycle when attribution was first built.

So the state goes through the filesystem. That is not a workaround; it is the
same choice the rate limiter already makes for its rolling window
(`RateLimiter(state_path=...)`), for the same reason: a value that must be
shared by processes and survive a restart is a file.

Two properties matter and both are cheap:

  * **Single writer.** Only the main process updates, because only the main
    process sees rewards. Workers read. That removes the hard half of the
    concurrency problem before it starts.
  * **Atomic replace.** A reader must never see a half-written file. Writing to
    a temporary path and `os.replace`-ing it means a worker either sees the
    previous state or the next one, never a truncated one.

A missing or unreadable file is not an error. It means "no evidence yet", the
selector starts from its prior, and the run proceeds — a bandit that refuses to
run because its history is missing would be worse than one that starts fresh.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict, Hashable, Optional, Sequence

from .bandit import ArmStats, Selector, build_selector

DEFAULT_SELECTOR = "discounted_thompson"


def _atomic_write(path: str, payload: Dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".bandit-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        # Leaving a stale temp file behind would be litter; leaving a
        # half-written state file would be a correctness bug. Clean up the
        # former and never create the latter.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def serialise(selector: Selector) -> Dict[str, Any]:
    """The learned state, in a form that survives a round trip through JSON."""
    return {
        "selector": selector.name,
        "arms": {
            str(arm): {
                "alpha": s.alpha, "beta": s.beta, "pulls": s.pulls,
                "total_reward": s.total_reward, "last_reward": s.last_reward,
            }
            for arm, s in selector.stats.items()
        },
    }


def deserialise(state: Dict[str, Any], arms: Sequence[Hashable],
                **kw: Any) -> Selector:
    """
    Rebuild a selector, keeping only evidence about arms that still exist.

    Dropping unknown arms matters when the operator taxonomy changes: a
    posterior learned for an operator that has since been renamed or removed
    would otherwise sit in the state file forever, and `select()` could return
    an arm the caller cannot act on.
    """
    name = state.get("selector") or DEFAULT_SELECTOR
    try:
        selector = build_selector(name, arms, **kw)
    except ValueError:
        selector = build_selector(DEFAULT_SELECTOR, arms, **kw)

    known = {str(a): a for a in selector.arms}
    for arm_key, raw in (state.get("arms") or {}).items():
        arm = known.get(arm_key)
        if arm is None or not isinstance(raw, dict):
            continue
        try:
            selector.stats[arm] = ArmStats(
                alpha=float(raw.get("alpha", 1.0)),
                beta=float(raw.get("beta", 1.0)),
                pulls=int(raw.get("pulls", 0)),
                total_reward=float(raw.get("total_reward", 0.0)),
                last_reward=(float(raw["last_reward"])
                             if raw.get("last_reward") is not None else None),
            )
        except (TypeError, ValueError):
            # One malformed arm must not discard the evidence for the others.
            continue
    return selector


class BanditStore:
    """
    File-backed bandit shared between the main process and its workers.

    Every method is safe to call when the file is missing, unreadable, or
    written by a different version of this code. The run matters more than the
    bandit: a selection failure falls back to the caller's default, and a
    persistence failure is dropped rather than raised.
    """

    def __init__(self, path: str, arms: Sequence[Hashable],
                 *, selector: str = DEFAULT_SELECTOR, **selector_kw: Any) -> None:
        self.path = path
        self.arms = list(arms)
        self.selector_name = selector
        self.selector_kw = dict(selector_kw)

    # -- reading (workers) ---------------------------------------------

    def load(self) -> Selector:
        """The current selector, or a fresh one if there is no usable state."""
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            return build_selector(self.selector_name, self.arms, **self.selector_kw)
        if not isinstance(state, dict):
            return build_selector(self.selector_name, self.arms, **self.selector_kw)
        return deserialise(state, self.arms, **self.selector_kw)

    def select(self, candidates: Optional[Sequence[Hashable]] = None) -> Optional[Hashable]:
        """Pick an arm, or None if the bandit cannot answer."""
        try:
            return self.load().select(candidates)
        except (ValueError, KeyError):
            return None

    # -- writing (main process only) -----------------------------------

    def update(self, arm: Hashable, reward: float) -> bool:
        """
        Fold one observation in and persist. Returns whether it was recorded.

        Read-modify-write is safe here only because the main process is the
        sole writer, and single-threaded within it. Both halves were checked
        rather than assumed:

          * `ProgramDatabase.add` is called from one place in the engine's
            process-parallel loop (`process_parallel.py`), which pops one
            completed future at a time inside a single asyncio task.
          * `_reward_operator` is fully synchronous, so no `await` can split
            this read from its write and let another iteration interleave.

        `parallel_evaluations` parallelises the worker pool, not `add`. If a
        second writer is ever introduced this needs a lock, and the symptom of
        getting it wrong would be lost updates rather than a crash — which is
        to say, silent.
        """
        try:
            selector = self.load()
            selector.ensure_arm(arm)
            selector.update(arm, reward)
            _atomic_write(self.path, serialise(selector))
            return True
        except (OSError, ValueError, TypeError):
            return False

    def snapshot(self) -> Dict[str, Any]:
        """What the bandit currently believes, for the dashboard and the log."""
        try:
            snap = self.load().snapshot()
        except (OSError, ValueError):
            return {"selector": self.selector_name, "arms": {}, "total_pulls": 0,
                    "path": self.path, "readable": False}
        snap["path"] = self.path
        snap["readable"] = True
        return snap

    def reset(self) -> None:
        try:
            os.unlink(self.path)
        except OSError:
            pass
