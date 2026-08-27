"""Worker integration: direct async tests + subprocess hello / no-api-key."""
import json
import subprocess
import sys
import time
import pathlib
import asyncio
import os


def test_worker_hello_subprocess():
    proc = subprocess.Popen(
        [sys.executable, "-m", "oe_max.brain.worker", "--brain", "null"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        hello_line = proc.stdout.readline()
        assert hello_line, "no hello"
        hello = json.loads(hello_line)
        assert hello["type"] == "hello"
        assert "worker_version" in hello
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def test_no_api_key_required():
    env = dict(os.environ)
    env.pop("NVIDIA_API_KEY", None)
    env.pop("OPENCODE_API_KEY", None)
    env.pop("OPENCODE_ZEN_API_KEY", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "oe_max.brain.worker", "--brain", "null"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        bufsize=1,
    )
    try:
        hello = json.loads(proc.stdout.readline())
        assert hello["type"] == "hello"
        # Direct brain/generate via _handle_rpc without subprocess roundtrip
        from oe_max.brain.worker import state
        from oe_max.brain.port import NullBrainPort
        from oe_max.brain.capabilities import BrainCapabilities
        import oe_max.brain.worker as w
        async def _t():
            old = w.state.brain
            w.state.brain = NullBrainPort()
            w.state.capabilities = BrainCapabilities.minimal()
            r = await w._handle_rpc("brain/generate", {"request": {"objective": "test", "policy": "general"}})
            w.state.brain = old
            return r
        r = asyncio.run(_t())
        assert "response" in r
        assert r["response"]["ok"] is True
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def test_worker_lifecycle_direct():
    # Direct async calls avoid subprocess pipe deadlock on Windows
    from oe_max.brain.worker import state
    from oe_max.brain.port import NullBrainPort
    from oe_max.brain.capabilities import BrainCapabilities
    import oe_max.brain.worker as w

    async def _run():
        # Reset state
        w.state.runs.clear()
        w.state.evolution_tasks.clear()
        w.state.evolution_stats.clear()
        w.state.brain = NullBrainPort(stub="def solve(x):\n    return x*2\n")
        w.state.capabilities = BrainCapabilities.minimal()

        r = await w._handle_rpc("evolve/start", {"goal": "minimize function", "iterations": 3, "seed": 1})
        run_id = r["run_id"]
        assert run_id

        # Wait for background task
        for _ in range(20):
            await asyncio.sleep(0.3)
            s = await w._handle_rpc("evolve/status", {"run_id": run_id})
            run = s.get("run")
            assert run is not None
            if run.get("status") in ("completed", "failed", "cancelled"):
                break

        ins = await w._handle_rpc("evolve/inspect", {"run_id": run_id})
        assert ins["run"]["run_id"] == run_id
        assert "capabilities" in ins

        c = await w._handle_rpc("evolve/candidates", {"run_id": run_id, "limit": 10})
        assert "candidates" in c

        cand_id = c["candidates"][0]["candidate"] if c["candidates"] else "dummy"
        a = await w._handle_rpc("evolve/apply", {"run_id": run_id, "candidate_id": cand_id, "dry_run": True})
        assert "run_id" in a

        st = await w._handle_rpc("evolve/stop", {"run_id": run_id})
        assert st["ok"]

        h = await w._handle_rpc("brain/health", {})
        assert "healthy" in h
        return True

    assert asyncio.run(_run()) is True


def test_isolation_explicit_promotion_direct():
    from oe_max.brain.worker import state
    from oe_max.brain.port import NullBrainPort
    from oe_max.brain.capabilities import BrainCapabilities
    import oe_max.brain.worker as w

    async def _run():
        w.state.runs.clear()
        w.state.evolution_tasks.clear()
        w.state.evolution_stats.clear()
        w.state.brain = NullBrainPort()
        w.state.capabilities = BrainCapabilities.minimal()

        r = await w._handle_rpc("evolve/start", {"goal": "test", "iterations": 2})
        run_id = r["run_id"]
        await asyncio.sleep(2)
        s = await w._handle_rpc("evolve/status", {"run_id": run_id})
        assert s["run"]["status"] in ("running", "completed", "failed")
        c = await w._handle_rpc("evolve/candidates", {"run_id": run_id})
        if c["candidates"]:
            cand = c["candidates"][0]["candidate"]
            a = await w._handle_rpc("evolve/apply", {"run_id": run_id, "candidate_id": cand, "dry_run": False})
            assert a["status"] == "applied"
            assert a["explicit"] is True
        return True

    assert asyncio.run(_run()) is True
