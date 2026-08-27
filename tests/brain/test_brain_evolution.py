"""Evolution hardening: caching, funnel, isolation, budgets, checkpoint, cancellation."""

import time
import pathlib
import tempfile
import asyncio
import sys


def test_content_cache_basic():
    from oe_max.brain.cache import ContentCache

    c = ContentCache(max_entries=10)
    k1 = c.make_key(base_sha="abc", patch="diff1", config={"a": 1})
    k2 = c.make_key(base_sha="abc", patch="diff1", config={"a": 1})
    k3 = c.make_key(base_sha="abc", patch="diff2", config={"a": 1})
    assert k1 == k2
    assert k1 != k3

    assert c.get(k1) is None
    c.put(k1, {"score": 1.0})
    assert c.get(k1) == {"score": 1.0}
    assert c.hit_rate > 0
    assert c.stats()["size"] == 1


def test_content_cache_prevents_rerun():
    from oe_max.brain.cache import ContentCache

    c = ContentCache(max_entries=10)
    patch = "diff --git a/foo.py b/foo.py\n+print('hello')"
    k = c.make_key(base_sha="deadbeef", patch=patch, evaluator_version="v2", benchmark_version="v1")
    c.put(k, {"evaluated": True, "score": 0.9})
    # Second discovery of same patch should hit cache, not rerun evaluator
    assert c.get(k) is not None
    assert c.get(k)["score"] == 0.9


def test_funnel_cheap_death():
    from oe_max.brain.funnel import Funnel, FunnelConfig, Stage, FunnelResult

    def g0(code, ctx):
        if "syntax error" in code:
            return FunnelResult(passed=False, stage=Stage.G0_VALIDITY, reason="parse fail")
        return FunnelResult(passed=True, stage=Stage.G0_VALIDITY)

    def expensive(code, ctx):
        # Should never run if G0 fails
        raise AssertionError("expensive stage should not run after cheap failure")

    funnel = Funnel(FunnelConfig(enabled_stages=[Stage.G0_VALIDITY, Stage.EXPENSIVE_BENCHMARK]))
    funnel.register(Stage.G0_VALIDITY, g0)
    funnel.register(Stage.EXPENSIVE_BENCHMARK, expensive)

    results = funnel.run("syntax error here")
    assert not funnel.passed(results)
    assert funnel.failed_at(results) == Stage.G0_VALIDITY
    # Only one stage ran (cheap), expensive was skipped
    assert len(results) == 1


def test_gates_integration():
    from oe_max.evaluation.gates import g0_validity, g1_dedup  # existing cheap gates

    # G0 should reject empty code
    r = g0_validity("", required_functions=None)
    assert not r.passed

    # G0 should pass valid python
    r = g0_validity("def foo():\n    return 42\n")
    assert r.passed


def test_isolation_does_not_corrupt_real_tree():
    from oe_max.brain.isolation import isolated_worktree, apply_patch
    import pathlib

    with tempfile.TemporaryDirectory() as tmp:
        repo = pathlib.Path(tmp) / "repo"
        repo.mkdir()
        # Create a git repo
        import subprocess

        subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=str(repo), capture_output=True)
        (repo / "hello.py").write_text("print('original')\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(repo), capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True)

        original = (repo / "hello.py").read_text(encoding="utf-8")

        # Evaluate candidate in isolated worktree — mutate hello.py
        with isolated_worktree(repo) as wt:
            assert wt != repo
            assert (wt / "hello.py").exists()
            # Apply a patch inside worktree only
            patch = """--- a/hello.py
+++ b/hello.py
@@ -1 +1 @@
-print('original')
+print('mutated')
"""
            ok = apply_patch(wt, patch)
            # Even if patch apply fails due to missing git, the worktree file can be edited directly
            if not ok:
                (wt / "hello.py").write_text("print('mutated')\n", encoding="utf-8")
            # Worktree is mutated
            assert (wt / "hello.py").read_text(encoding="utf-8") != original

        # After worktree context exits, real repo must be unchanged
        assert (repo / "hello.py").read_text(encoding="utf-8") == original


def test_checkpoint_crash_resume():
    from oe_max.brain.checkpoint import Checkpoint, CheckpointStore

    with tempfile.TemporaryDirectory() as tmp:
        store = CheckpointStore(pathlib.Path(tmp))
        cp = Checkpoint(experiment_id="exp-1", goal="optimize foo", base_sha="abc123", generation=5)
        cp.candidates.append({"id": "c1", "score": 0.9})
        store.save(cp)

        # Simulate crash — load latest
        loaded = store.load("exp-1")
        assert loaded is not None
        assert loaded.experiment_id == "exp-1"
        assert loaded.generation == 5
        assert len(loaded.candidates) == 1

        # Resume: bump generation and save again
        loaded.generation += 1
        loaded.candidates.append({"id": "c2", "score": 0.95})
        store.save(loaded)
        reloaded = store.load("exp-1")
        assert reloaded.generation == 6
        assert len(reloaded.candidates) == 2


def test_budgets_exhaustion():
    from oe_max.brain.budgets import BudgetConfig, BudgetState

    cfg = BudgetConfig(candidate_budget=2, wall_clock_budget_s=10.0)
    state = BudgetState(config=cfg)
    assert state.exhausted() is None
    state.candidates_evaluated = 2
    assert state.exhausted() == "candidate_budget"

    cfg2 = BudgetConfig(wall_clock_budget_s=0.01)
    state2 = BudgetState(config=cfg2, started_at=time.time() - 1.0)
    assert state2.exhausted() == "wall_clock_budget"


def test_cancellation_via_timeout():
    from oe_max.brain.port import BrainPort
    from oe_max.brain.types import BrainRequest
    from oe_max.brain.budgets import GenericBackoff

    async def _run():
        # Null port should respect timeout
        from oe_max.brain.port import NullBrainPort

        brain = NullBrainPort(stub="hello")
        req = BrainRequest(objective="test")
        # Should succeed quickly
        resp = await asyncio.wait_for(brain.generate(req), timeout=1.0)
        assert resp.ok
        # Cancellation via wait_for timeout
        try:
            await asyncio.wait_for(asyncio.sleep(2), timeout=0.1)
            assert False, "should have timed out"
        except asyncio.TimeoutError:
            pass

    asyncio.run(_run())


def test_plugin_tools_present():
    # The source, not dist/: build output is not committed, so a check against
    # it passes only where someone happened to run tsc.
    plugin_ts = (pathlib.Path(__file__).resolve().parents[2]
                 / "packages" / "opencode-plugin" / "src" / "index.ts")
    assert plugin_ts.exists()
    text = plugin_ts.read_text(encoding="utf-8", errors="ignore")
    for t in ["evolve_start", "evolve_stop", "evolve_pause", "evolve_resume", "evolve_inspect", "evolve_candidates", "evolve_apply"]:
        assert t in text


def test_brain_mode_inherit_default():
    # Worker should default to stdio (inherit), not legacy
    from pathlib import Path

    worker = Path(__file__).resolve().parents[2] / "oe_max" / "brain" / "worker.py"
    text = worker.read_text(encoding="utf-8")
    # Default brain should be stdio (inherit), legacy is opt-in
    assert "--brain" in text
    # Check that default is stdio, not legacy
    assert 'default="stdio"' in text or "default='stdio'" in text
