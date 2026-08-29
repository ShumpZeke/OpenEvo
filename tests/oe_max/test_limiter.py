"""
Proof tests for the global rate limiter.

The build spec states the invariant as absolute:

    FOR EVERY CONTIGUOUS 60-SECOND WINDOW: NIM ATTEMPT STARTS <= 44

so these tests check *every* contiguous window over recorded attempt starts, not
just aggregate throughput. Aggregate rate can look fine while a burst straddling
a window boundary still violates the contract.

All of it runs on a virtual clock: thousands of attempts across simulated
minutes, deterministically, in milliseconds.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from oe_max.limiter import NullLimiter, RateLimiter, VirtualClock


def assert_window_invariant(starts, window=60.0, cap=44):
    """
    Check every contiguous window, anchored at each attempt start.

    Anchoring at each start is sufficient: any window containing more than `cap`
    starts can be slid left until its left edge meets a start without losing
    any of them, so a violation always shows up at some anchor.
    """
    starts = sorted(starts)
    n = len(starts)
    j = 0
    worst = 0
    for i in range(n):
        while j < n and starts[j] < starts[i] + window:
            j += 1
        count = j - i
        worst = max(worst, count)
        assert count <= cap, (
            f"window starting at t={starts[i]:.3f} contains {count} attempt "
            f"starts (cap {cap}); starts={starts[i:j][:50]}"
        )
    return worst


def make(clock, **kw):
    return RateLimiter(
        "nim", hard_cap_per_window=kw.pop("cap", 44), window_seconds=60.0,
        target_rpm=kw.pop("rpm", 42.0), burst_capacity=kw.pop("burst", 2.0),
        clock=clock.time, sleep=clock.sleep, **kw,
    )


@pytest.mark.asyncio
async def test_single_worker_never_exceeds_the_cap():
    clock = VirtualClock()
    lim = make(clock)
    starts = []
    for _ in range(300):
        await lim.acquire()
        starts.append(clock.now)
    worst = assert_window_invariant(starts)
    assert worst > 0


@pytest.mark.asyncio
async def test_many_concurrent_workers_share_one_budget():
    """
    The failure this guards against: a per-worker limiter. 20 workers each
    politely limited to 44/min would issue 880/min against one account.
    """
    clock = VirtualClock()
    lim = make(clock)
    starts = []

    async def worker(n):
        for _ in range(n):
            await lim.acquire()
            starts.append(clock.now)

    await asyncio.gather(*(worker(15) for _ in range(20)))  # 300 attempts, 20 workers
    assert len(starts) == 300
    assert_window_invariant(starts)


@pytest.mark.asyncio
async def test_retries_count_against_the_budget():
    """Retries are attempts. A retry path that skips acquire() breaks the contract."""
    clock = VirtualClock()
    lim = make(clock)
    starts = []
    for _ in range(60):
        await lim.acquire()          # initial attempt
        starts.append(clock.now)
        for _ in range(2):           # two retries, each acquiring again
            await lim.acquire()
            starts.append(clock.now)
    assert len(starts) == 180
    assert_window_invariant(starts)


@pytest.mark.asyncio
async def test_burst_then_idle_then_burst_across_window_boundary():
    """
    The boundary case a token bucket alone gets wrong: fill the window, idle
    just under 60s, then burst again. Naive accounting resets at the wrong
    moment and briefly doubles the rate.
    """
    clock = VirtualClock()
    lim = make(clock, burst=44)   # generous bucket so only the window guard binds
    starts = []
    for _ in range(44):
        await lim.acquire()
        starts.append(clock.now)
    clock.advance(59.0)           # not yet a full window
    for _ in range(44):
        await lim.acquire()
        starts.append(clock.now)
    assert_window_invariant(starts)


@pytest.mark.asyncio
async def test_cap_is_reached_and_not_merely_undershot():
    """
    A limiter that grants nothing also satisfies the bound. Assert we actually
    use the budget: over several minutes the busiest window should approach it.
    """
    clock = VirtualClock()
    lim = make(clock, burst=10)
    starts = []
    for _ in range(400):
        await lim.acquire()
        starts.append(clock.now)
    worst = assert_window_invariant(starts)
    assert worst >= 35, f"limiter is far too conservative: peak window {worst}/44"


@pytest.mark.asyncio
async def test_throughput_tracks_target_rate():
    clock = VirtualClock()
    lim = make(clock, rpm=42.0, burst=2)
    n = 420
    for _ in range(n):
        await lim.acquire()
    elapsed_minutes = clock.now / 60.0
    observed = n / max(elapsed_minutes, 1e-9)
    assert 30.0 <= observed <= 44.0, f"observed {observed:.1f} rpm"


@pytest.mark.asyncio
async def test_penalty_slows_dispatch_globally():
    clock = VirtualClock()
    lim = make(clock, burst=1)
    for _ in range(10):
        await lim.acquire()
    baseline = clock.now

    lim.penalise(retry_after=5.0, factor=4.0, duration=60.0)
    t0 = clock.now
    for _ in range(10):
        await lim.acquire()
    penalised = clock.now - t0
    assert penalised > baseline, "a 429 penalty must actually slow dispatch"


@pytest.mark.asyncio
async def test_penalty_expires_and_rate_recovers():
    clock = VirtualClock()
    lim = make(clock, burst=1)
    lim.penalise(factor=4.0, duration=10.0)
    clock.advance(11.0)
    assert lim.snapshot()["penalty_factor"] == 1.0


@pytest.mark.asyncio
async def test_cancellation_does_not_consume_a_slot():
    """
    A waiter cancelled before it is granted must not have committed an attempt.

    Asserted via `granted`, not `window_count`: on the virtual clock the
    cancelled waiter's sleep advances shared time, which legitimately ages the
    window out. What must not happen is a slot being *charged* for a request
    that never started — that would drift the accounting and silently
    under-use the contract over a long run.
    """
    clock = VirtualClock()
    lim = make(clock, cap=5, burst=5)
    for _ in range(5):
        await lim.acquire()
    granted_before = lim.stats.granted
    assert granted_before == 5

    task = asyncio.create_task(lim.acquire())   # window is full; this must wait
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert lim.stats.granted == granted_before, "cancelled attempt was charged a slot"


@pytest.mark.asyncio
async def test_window_count_and_headroom_are_reported():
    clock = VirtualClock()
    lim = make(clock, cap=44, burst=44)
    for _ in range(10):
        await lim.acquire()
    snap = lim.snapshot()
    assert snap["window_count"] == 10
    assert snap["headroom"] == 34
    clock.advance(61.0)
    assert lim.window_count() == 0


@pytest.mark.asyncio
async def test_stress_mixed_workers_bursts_and_retries():
    """Everything at once: uneven workers, bursts, retries, idle gaps."""
    clock = VirtualClock()
    lim = make(clock)
    starts = []

    async def bursty(count, retries):
        for _ in range(count):
            await lim.acquire()
            starts.append(clock.now)
            for _ in range(retries):
                await lim.acquire()
                starts.append(clock.now)

    await asyncio.gather(
        bursty(30, 0), bursty(20, 1), bursty(10, 2), bursty(40, 0),
    )
    assert_window_invariant(starts)
    assert lim.stats.granted == len(starts)


@pytest.mark.asyncio
async def test_zero_capacity_configuration_is_rejected():
    clock = VirtualClock()
    with pytest.raises(ValueError):
        RateLimiter("bad", hard_cap_per_window=0, clock=clock.time, sleep=clock.sleep)
    with pytest.raises(ValueError):
        RateLimiter("bad", target_rpm=0, clock=clock.time, sleep=clock.sleep)


@pytest.mark.asyncio
async def test_null_limiter_is_unlimited_but_counts():
    lim = NullLimiter("zen")
    for _ in range(100):
        assert await lim.acquire() == 0.0
    assert lim.stats.granted == 100
    assert lim.snapshot()["unlimited"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("cap,rpm", [(44, 42.0), (10, 10.0), (100, 90.0), (1, 1.0)])
async def test_invariant_holds_across_configurations(cap, rpm):
    clock = VirtualClock()
    lim = make(clock, cap=cap, rpm=rpm, burst=min(5, cap))
    starts = []
    for _ in range(cap * 4):
        await lim.acquire()
        starts.append(clock.now)
    assert_window_invariant(starts, cap=cap)


# ---------------------------------------------------------------- durability
@pytest.mark.asyncio
async def test_window_survives_a_process_restart(tmp_path):
    """
    The gap this closes: without persistence a restart forgets the rolling
    window, and a burst immediately afterwards exceeds the contract — exactly
    when a restart is most likely (a crash loop under load).
    """
    state = str(tmp_path / "nim.window")

    first = RateLimiter("nim", hard_cap_per_window=44, target_rpm=42.0,
                        burst_capacity=44, state_path=state)
    for _ in range(20):
        await first.acquire()
    assert first.window_count() == 20

    # A fresh process, same state file.
    second = RateLimiter("nim", hard_cap_per_window=44, target_rpm=42.0,
                         burst_capacity=44, state_path=state)
    assert second.window_count() == 20, "restart forgot the window"
    assert second.snapshot()["headroom"] == 24


@pytest.mark.asyncio
async def test_expired_starts_are_not_restored(tmp_path):
    state = tmp_path / "old.window"
    stale = time.time() - 3600          # an hour old, far outside the window
    state.write_text(f"{stale}\n" * 40)
    lim = RateLimiter("nim", hard_cap_per_window=44, state_path=str(state))
    assert lim.window_count() == 0


@pytest.mark.asyncio
async def test_corrupt_state_cannot_wedge_the_limiter(tmp_path):
    """
    A malformed or hostile file must not be able to jam the limiter shut, and
    must not crash it. Unreadable lines are discarded, and the restore is
    capped so a file claiming thousands of starts cannot block all traffic.
    """
    state = tmp_path / "corrupt.window"
    now = time.time()
    state.write_text("not-a-number\n\n" + f"{now}\n" * 500 + "\x00garbage\n")
    lim = RateLimiter("nim", hard_cap_per_window=44, state_path=str(state))
    assert lim.window_count() <= 44
    assert lim.snapshot()["persistent"] is True


@pytest.mark.asyncio
async def test_missing_state_file_is_not_an_error(tmp_path):
    lim = RateLimiter("nim", state_path=str(tmp_path / "does" / "not" / "exist"))
    assert lim.window_count() == 0
    await lim.acquire()          # must still work, and create the file
    assert lim.stats.granted == 1


@pytest.mark.asyncio
async def test_compaction_drops_aged_out_entries(tmp_path):
    state = tmp_path / "compact.window"
    lim = RateLimiter("nim", hard_cap_per_window=44, burst_capacity=44,
                      state_path=str(state))
    for _ in range(10):
        await lim.acquire()
    lines_before = len(state.read_text().splitlines())
    assert lines_before == 10

    # Rewrite them as old, then compact.
    old = time.time() - 3600
    state.write_text(f"{old}\n" * 10)
    lim.compact_state()
    assert state.read_text().strip() == ""


@pytest.mark.asyncio
async def test_persistence_is_optional_and_off_by_default():
    lim = RateLimiter("zen")
    assert lim.snapshot()["persistent"] is False


# -- the shipped bound ------------------------------------------------------


def test_the_shipped_default_is_forty():
    """The operator set 40 on 2026-08-29, below both the 48 the account allows
    and the 44 the spec required.

    Asserted on the *defaults* rather than on a limiter built with explicit
    arguments, because every other test here passes its own cap -- so all of
    them would keep passing if the shipped number silently changed.
    """
    import inspect

    from oe_max.limiter import RateLimiter
    from oe_max.providers.registry import build_default_registry

    assert inspect.signature(RateLimiter.__init__) \
        .parameters["hard_cap_per_window"].default == 40
    assert inspect.signature(RateLimiter.__init__) \
        .parameters["target_rpm"].default == 38.0

    registry_sig = inspect.signature(build_default_registry)
    assert registry_sig.parameters["nim_hard_cap"].default == 40
    assert registry_sig.parameters["nim_target_rpm"].default == 38.0


@pytest.mark.asyncio
async def test_the_default_bound_holds_under_load():
    """40 is the number; this is the guarantee.

    Twelve concurrent workers hammering the default limiter -- the shape the
    real thing runs in, where retries, health probes and critic calls all
    contend -- and no contiguous 60-second window may contain more than 40
    attempt starts. Built with no explicit cap so it measures what ships.
    """
    import asyncio

    clock = VirtualClock()
    limiter = RateLimiter(
        "nim", window_seconds=60.0, clock=clock.time, sleep=clock.sleep,
    )
    starts = []

    async def worker():
        for _ in range(20):
            await limiter.acquire()
            starts.append(clock.time())

    await asyncio.gather(*(worker() for _ in range(12)))

    worst = assert_window_invariant(starts, cap=40)
    assert len(starts) == 240
    # And not so conservative it would be useless -- a limiter that let three
    # requests a minute through would also satisfy the bound above.
    assert worst >= 30, f"peak window only {worst}/40; the limiter is throttling far below its cap"
