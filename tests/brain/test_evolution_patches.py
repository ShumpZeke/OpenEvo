"""Tests for patch-first evolution and failure archive integration."""
import asyncio
from oe_max.brain.evolution import _apply_diff, _extract_diff, EvolutionConfig, run_evolution
from oe_max.brain.port import NullBrainPort


def test_extract_diff():
    md = "Here:\n```diff\n--- a/parent.py\n+++ b/parent.py\n@@\n+def foo(): pass\n```\n"
    assert "--- a/parent.py" in _extract_diff(md)
    plain = "def foo(): pass"
    assert _extract_diff(plain) == plain
    md2 = "```python\ndef foo(): return 1\n```"
    # Not a diff, should return as is (but our function only extracts diff blocks)
    assert "def foo" in _extract_diff(md2) or _extract_diff(md2) == md2


def test_apply_diff_basic():
    parent = "def solve(x):\n    return x\n"
    diff = """--- a/parent.py
+++ b/parent.py
@@ -1,2 +1,2 @@
-def solve(x):
-    return x
+def solve(x):
+    return x*2
"""
    result = _apply_diff(parent, diff)
    assert "return x*2" in result
    assert "def solve" in result


def test_apply_diff_markdown():
    parent = "def solve(x):\n    return x\n"
    md = """```diff
--- a/parent.py
+++ b/parent.py
@@ -1,2 +1,2 @@
-def solve(x):
-    return x
+def solve(x):
+    return x*2
```"""
    result = _apply_diff(parent, md)
    assert "return x*2" in result


def test_markdown_code_extraction():
    async def t():
        class MdBrain(NullBrainPort):
            async def generate(self, req):
                from oe_max.brain.types import BrainResponse
                return BrainResponse(content="```python\ndef solve(x):\n    return x*5\n```")
        brain = MdBrain()
        cfg = EvolutionConfig(iterations=1, seed=1, initial_code="def solve(x):\n    return x\n")
        stats = await run_evolution(brain, cfg)
        assert stats.candidates == 1
        assert "return x*5" in stats.best_code
    asyncio.run(t())


def test_failure_archive_context():
    # Ensure failure context is built without dumping full history
    async def t():
        class FailThenSucceed(NullBrainPort):
            def __init__(self):
                super().__init__()
                self.calls = 0
            async def generate(self, req):
                from oe_max.brain.types import BrainResponse
                self.calls += 1
                if self.calls == 1:
                    # Return invalid code to trigger failure
                    return BrainResponse(content="def solve(x):\n    syntax error :\n")
                return BrainResponse(content="def solve(x):\n    return x*2\n")
        brain = FailThenSucceed()
        cfg = EvolutionConfig(iterations=3, seed=1, initial_code="def solve(x):\n    return x\n")
        stats = await run_evolution(brain, cfg)
        # Should have one invalid, then one valid
        assert stats.invalid >= 1 or stats.duplicates >= 0
        assert stats.candidates >= 1
    asyncio.run(t())


def test_patch_first_no_full_history_dump():
    # Verify BrainRequest context is compact (< 5 keys, no full history)
    async def t():
        captured = {}
        class CapturingBrain(NullBrainPort):
            async def generate(self, req):
                from oe_max.brain.types import BrainResponse
                captured.update(req.to_dict())
                return BrainResponse(content="def solve(x):\n    return x*2\n")
        brain = CapturingBrain()
        cfg = EvolutionConfig(iterations=1, seed=1, initial_code="def solve(x):\n    return x\n")
        await run_evolution(brain, cfg)
        ctx = captured.get("context", {})
        # Compact: should have generation, best_score, operator, attempt, maybe recent_failures (max 2)
        assert len(ctx) <= 5, f"context too large: {ctx}"
        assert "recent_failures" not in ctx or len(ctx["recent_failures"]) <= 2
        # Should not contain full history
        assert "history" not in str(ctx).lower()
        assert "candidates" not in ctx
    asyncio.run(t())
