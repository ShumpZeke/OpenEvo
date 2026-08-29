"""
DEPRECATED — NVIDIA-specific rate limiting, superseded by oe_max.brain.budgets.

New code must use generic Budgets (max_brain_inflight, token_budget, etc.).
Provider-specific throttling belongs outside the core.
See oe_max/brain/README.md.

Global provider rate limiting.

The NVIDIA NIM account carries a hard contract of 48 requests/minute. The build
spec made the invariant explicit and absolute, at 44:

    FOR EVERY CONTIGUOUS 60-SECOND WINDOW:
        NIM ATTEMPT STARTS <= 40

The bound is now **40**, set by the operator on 2026-08-29. It is stricter than
both the 48 the provider allows and the 44 the spec required, so nothing that
held before stops holding -- a run bounded by 40 is bounded by 44. The number is
a default and a parameter, not a constant: `build_default_registry` takes
`nim_hard_cap`, so an account with a different contract passes its own.

"Attempt starts", not "successful requests": retries, health probes, critic
calls and cancelled-in-flight attempts all count. There is no emergency bypass.

Two mechanisms are required and both are implemented, because either alone is
insufficient:

  1. **Rolling-window guard** — the exact invariant. Before an attempt starts we
     record its timestamp; if the last 60 seconds already hold `hard_cap`
     starts, we sleep until the oldest one ages out. This is what makes the
     bound provable rather than statistical.

  2. **Token bucket** — smooths dispatch to `target_rpm` so we arrive at the
     cap gently instead of firing 44 requests in two seconds and then stalling
     for 58. A pure window guard is bursty; a pure bucket can drift over a
     window boundary. Together they are both bounded and evenly paced.

Time is injected (`clock`/`sleep`) so the invariant can be property-tested on a
deterministic virtual clock rather than by sleeping through real minutes.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Deque, Dict, Optional

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]

# Float slack for token comparisons, and the smallest wait that is allowed to
# be returned. Both exist to guarantee the acquire() loop always progresses.
_EPS = 1e-9
_MIN_WAIT = 1e-4


@dataclass
class LimiterStats:
    attempts: int = 0
    granted: int = 0
    waited: int = 0
    total_wait_s: float = 0.0
    max_wait_s: float = 0.0
    throttle_events: int = 0
    current_window_count: int = 0
    peak_window_count: int = 0

    def to_dict(self) -> Dict[str, float]:
        return dict(self.__dict__)


class RateLimiter:
    """
    Global, provider-scoped limiter enforcing a hard rolling-window cap.

    Every caller for a given provider must share one instance — that is the
    whole point. A per-worker limiter would multiply the effective rate by the
    worker count and silently blow the contract.
    """

    def __init__(
        self,
        name: str,
        *,
        hard_cap_per_window: int = 40,
        window_seconds: float = 60.0,
        target_rpm: float = 38.0,
        burst_capacity: float = 2.0,
        clock: Optional[Clock] = None,
        sleep: Optional[Sleeper] = None,
        state_path: Optional[str] = None,
    ) -> None:
        if hard_cap_per_window <= 0:
            raise ValueError("hard_cap_per_window must be positive")
        if target_rpm <= 0:
            raise ValueError("target_rpm must be positive")

        self.name = name
        self.hard_cap = hard_cap_per_window
        self.window = window_seconds
        self.target_rpm = target_rpm
        self.burst_capacity = max(1.0, burst_capacity)

        self._clock: Clock = clock or time.monotonic
        self._sleep: Sleeper = sleep or asyncio.sleep

        # Attempt-start timestamps inside the current window.
        self._starts: Deque[float] = deque()

        # Token bucket state.
        self._tokens: float = self.burst_capacity
        self._last_refill: float = self._clock()

        # Global slow-down applied after a 429/503 (see `penalise`).
        self._penalty_until: float = 0.0
        self._penalty_factor: float = 1.0

        self._lock = asyncio.Lock()
        self.stats = LimiterStats()

        # Optional durability. Without it a process restart forgets the rolling
        # window, and a burst immediately afterwards can exceed the contract —
        # precisely when a restart is most likely (a crash loop under load).
        # Timestamps are stored as wall-clock so they survive the monotonic
        # clock resetting; see _load_state for the conversion back.
        self._state_path = state_path
        if state_path:
            self._load_state()

    # -- internals -----------------------------------------------------

    def _prune(self, now: float) -> None:
        cutoff = now - self.window
        while self._starts and self._starts[0] <= cutoff:
            self._starts.popleft()

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._last_refill)
        self._last_refill = now
        rate_per_s = (self.target_rpm / 60.0) / self._current_penalty(now)
        self._tokens = min(self.burst_capacity, self._tokens + elapsed * rate_per_s)

    def _current_penalty(self, now: float) -> float:
        if now >= self._penalty_until:
            self._penalty_factor = 1.0
            return 1.0
        return self._penalty_factor

    def _wait_for_window(self, now: float) -> float:
        """Seconds until the rolling window has room. 0 if it has room now."""
        self._prune(now)
        if len(self._starts) < self.hard_cap:
            return 0.0
        # The oldest start must age past the window before another may begin.
        # Nudge past the boundary so a float equality cannot admit an extra one.
        return max((self._starts[0] + self.window) - now, 0.0) + _MIN_WAIT

    def _wait_for_token(self, now: float) -> float:
        self._refill(now)
        # Epsilon comparison: refilling by exactly the needed amount lands on
        # 0.9999999999 rather than 1.0, and a strict `>= 1.0` then computes a
        # ~1e-12 wait. Each loop iteration would sleep an invisible amount and
        # re-refill an invisible amount, spinning forever while appearing hung.
        if self._tokens >= 1.0 - _EPS:
            self._tokens = max(self._tokens, 1.0)
            return 0.0
        rate_per_s = (self.target_rpm / 60.0) / self._current_penalty(now)
        wait = (1.0 - self._tokens) / rate_per_s
        # Guarantee forward progress: never return a wait so small that it
        # cannot change the outcome on the next iteration.
        return max(wait, _MIN_WAIT)

    # -- public API ----------------------------------------------------

    async def acquire(self) -> float:
        """
        Block until this attempt may start, then record it.

        Returns the time waited. Must be called exactly once per *attempt*,
        including retries — the caller does not get to decide that a retry is
        "free".
        """
        total_wait = 0.0
        self.stats.attempts += 1

        while True:
            async with self._lock:
                now = self._clock()
                wait = max(self._wait_for_window(now), self._wait_for_token(now))
                if wait <= 0.0:
                    # Commit the attempt while still holding the lock, so two
                    # coroutines cannot both observe the same free slot.
                    self._starts.append(now)
                    self._persist(time.time())
                    self._tokens -= 1.0
                    self.stats.granted += 1
                    self.stats.current_window_count = len(self._starts)
                    self.stats.peak_window_count = max(
                        self.stats.peak_window_count, len(self._starts)
                    )
                    if total_wait > 0:
                        self.stats.waited += 1
                        self.stats.total_wait_s += total_wait
                        self.stats.max_wait_s = max(self.stats.max_wait_s, total_wait)
                    return total_wait

            # Sleep outside the lock so other coroutines can make progress.
            await self._sleep(wait)
            total_wait += wait

    # -- durability ----------------------------------------------------

    def _load_state(self) -> None:
        """
        Restore attempt starts still inside the window from a previous process.

        Conservative by construction: anything unreadable, malformed or of
        uncertain age is discarded rather than assumed safe, because the failure
        mode of guessing wrong is exceeding a hard contract.
        """
        path = self._state_path
        if not path or not os.path.exists(path):
            return
        try:
            now_wall = time.time()
            now_mono = self._clock()
            restored = []
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        wall = float(line)
                    except ValueError:
                        continue      # torn line from a killed process
                    age = now_wall - wall
                    if 0.0 <= age < self.window:
                        # Re-express on this process's monotonic timeline.
                        restored.append(now_mono - age)
            restored.sort()
            # Never restore more than the cap: a corrupted file must not be
            # able to wedge the limiter shut forever.
            self._starts.extend(restored[-self.hard_cap:])
        except OSError:
            return

    def _persist(self, start_wall: float) -> None:
        if not self._state_path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._state_path)) or ".",
                        exist_ok=True)
            with open(self._state_path, "a", encoding="utf-8") as fh:
                fh.write(f"{start_wall}\n")
        except OSError:
            # Durability is best-effort: failing to record must never block a
            # request that the in-memory window has already allowed.
            pass

    def compact_state(self) -> None:
        """Drop persisted starts that have aged out. Cheap; call periodically."""
        if not self._state_path or not os.path.exists(self._state_path):
            return
        try:
            cutoff = time.time() - self.window
            keep = []
            with open(self._state_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        w = float(line.strip())
                    except ValueError:
                        continue
                    if w > cutoff:
                        keep.append(w)
            tmp = self._state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.writelines(f"{w}\n" for w in keep[-self.hard_cap:])
            os.replace(tmp, self._state_path)
        except OSError:
            pass

    def penalise(self, retry_after: Optional[float] = None, factor: float = 2.0,
                 duration: float = 30.0) -> None:
        """
        Globally slow dispatch after a 429/503.

        Applies to the whole provider, not the one caller that got throttled:
        a rate limit is a statement about the account, so backing off a single
        worker while the rest keep firing would not help.
        """
        now = self._clock()
        self._penalty_factor = max(self._penalty_factor, factor)
        self._penalty_until = max(
            self._penalty_until, now + max(duration, retry_after or 0.0)
        )
        self.stats.throttle_events += 1

    def recover(self) -> None:
        """Gradually relax a penalty after sustained success."""
        if self._penalty_factor > 1.0:
            self._penalty_factor = max(1.0, self._penalty_factor / 1.5)

    def window_count(self, now: Optional[float] = None) -> int:
        now = now if now is not None else self._clock()
        self._prune(now)
        return len(self._starts)

    def snapshot(self) -> Dict[str, object]:
        now = self._clock()
        self._prune(now)
        return {
            "name": self.name,
            "hard_cap": self.hard_cap,
            "window_seconds": self.window,
            "target_rpm": self.target_rpm,
            "window_count": len(self._starts),
            "headroom": self.hard_cap - len(self._starts),
            "tokens": round(self._tokens, 3),
            "penalty_factor": round(self._current_penalty(now), 3),
            "penalty_remaining_s": round(max(0.0, self._penalty_until - now), 2),
            "persistent": bool(self._state_path),
            "stats": self.stats.to_dict(),
        }


class NullLimiter:
    """
    Unlimited pass-through for providers with no stated contract.

    Explicit rather than `Optional[RateLimiter]` at every call site: the spec
    says each provider gets its own scheduler, and a null object keeps callers
    from having to special-case "no limit" and accidentally skipping a real one.
    """

    def __init__(self, name: str = "unlimited") -> None:
        self.name = name
        self.stats = LimiterStats()

    async def acquire(self) -> float:
        self.stats.attempts += 1
        self.stats.granted += 1
        return 0.0

    # -- durability ----------------------------------------------------

    def _load_state(self) -> None:
        """
        Restore attempt starts still inside the window from a previous process.

        Conservative by construction: anything unreadable, malformed or of
        uncertain age is discarded rather than assumed safe, because the failure
        mode of guessing wrong is exceeding a hard contract.
        """
        path = self._state_path
        if not path or not os.path.exists(path):
            return
        try:
            now_wall = time.time()
            now_mono = self._clock()
            restored = []
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        wall = float(line)
                    except ValueError:
                        continue      # torn line from a killed process
                    age = now_wall - wall
                    if 0.0 <= age < self.window:
                        # Re-express on this process's monotonic timeline.
                        restored.append(now_mono - age)
            restored.sort()
            # Never restore more than the cap: a corrupted file must not be
            # able to wedge the limiter shut forever.
            self._starts.extend(restored[-self.hard_cap:])
        except OSError:
            return

    def _persist(self, start_wall: float) -> None:
        if not self._state_path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._state_path)) or ".",
                        exist_ok=True)
            with open(self._state_path, "a", encoding="utf-8") as fh:
                fh.write(f"{start_wall}\n")
        except OSError:
            # Durability is best-effort: failing to record must never block a
            # request that the in-memory window has already allowed.
            pass

    def compact_state(self) -> None:
        """Drop persisted starts that have aged out. Cheap; call periodically."""
        if not self._state_path or not os.path.exists(self._state_path):
            return
        try:
            cutoff = time.time() - self.window
            keep = []
            with open(self._state_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        w = float(line.strip())
                    except ValueError:
                        continue
                    if w > cutoff:
                        keep.append(w)
            tmp = self._state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.writelines(f"{w}\n" for w in keep[-self.hard_cap:])
            os.replace(tmp, self._state_path)
        except OSError:
            pass

    def penalise(self, retry_after: Optional[float] = None, factor: float = 2.0,
                 duration: float = 30.0) -> None:
        self.stats.throttle_events += 1

    def recover(self) -> None:
        pass

    def window_count(self, now: Optional[float] = None) -> int:
        return 0

    def snapshot(self) -> Dict[str, object]:
        return {"name": self.name, "unlimited": True, "stats": self.stats.to_dict()}


class VirtualClock:
    """
    Deterministic clock for testing the invariant without sleeping.

    `sleep` advances time instantly, so a test can drive thousands of attempts
    across simulated minutes and assert the window bound exactly.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.slept: list = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("negative sleep")
        self.slept.append(seconds)
        self.now += seconds
        # Yield so other coroutines run, mirroring real asyncio.sleep ordering.
        await asyncio.sleep(0)

    def advance(self, seconds: float) -> None:
        self.now += seconds
