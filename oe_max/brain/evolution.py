"""
Provider-neutral evolution loop — the core optimization engine.

Uses BrainPort as the only LLM source. No provider-specific code here.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import pathlib
import re
import random
import subprocess
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .budgets import BudgetConfig, BudgetState, GenericBackoff
from .cache import ContentCache
from .capabilities import BrainCapabilities
from .checkpoint import Checkpoint, CheckpointStore
from .funnel import Funnel, FunnelConfig, Stage, FunnelResult
from .isolation import isolated_worktree
from .port import BrainPort
from .types import BrainRequest, Operation, PolicyMode

try:
    from oe_max.search.operators import OPERATORS, OperatorClass
    from oe_max.search.bandit import Bandit  # may exist
    HAS_OE_MAX = True
except Exception:
    HAS_OE_MAX = False
    OPERATORS = {}  # type: ignore
    OperatorClass = None  # type: ignore

try:
    from oe_max.archives import HallOfFame, ParetoArchive, NoveltyArchive, FailureArchive
    HAS_ARCHIVES = True
except Exception:
    HAS_ARCHIVES = False

try:
    from oe_max.evaluation.gates import g0_validity, g1_dedup
    HAS_GATES = True
except Exception:
    HAS_GATES = False


@dataclass
class EvolutionConfig:
    iterations: int = 20
    seed: int = 42
    population_size: int = 4
    batch_size: int = 1  # candidates per generation (for successive halving)
    # Patch-first
    use_patch: bool = True
    # Successive halving
    halving_ratio: float = 0.33
    # Bandit
    use_bandit: bool = True
    # Budgets
    budgets: BudgetConfig = field(default_factory=BudgetConfig)
    # Checkpoint
    checkpoint_dir: Optional[Path] = None
    # Repo for isolation
    repo_root: Optional[Path] = None
    # Initial program
    initial_code: str = "def solve(x):\n    return x\n"


@dataclass
class EvolutionStats:
    generation: int = 0
    candidates: int = 0
    valid: int = 0
    duplicates: int = 0
    invalid: int = 0
    improvements: int = 0
    best_score: float = float("-inf")
    best_code: str = ""
    cache_hits: int = 0
    brain_calls: int = 0
    wall_s: float = 0.0
    operator_stats: Dict[str, Dict[str, int]] = field(default_factory=dict)


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()[:16]


def _extract_diff(text: str) -> str:
    """Extract diff from markdown fences if present."""
    if "```" in text:
        # Find first ```diff or ``` block containing diff headers
        m = re.search(r"```(?:diff)?\s*\n(.*?)\n```", text, re.DOTALL)
        if m and ("---" in m.group(1) or "diff --git" in m.group(1)):
            return m.group(1).strip()
    return text

def _apply_diff(parent_code: str, diff: str) -> str:
    """Try to apply a unified diff to parent_code, return new code or best-effort extracted code."""
    diff = _extract_diff(diff)
    if "diff --git" not in diff and "--- a/" not in diff and "---" not in diff:
        return diff
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            parent_file = tmp_path / "parent.py"
            parent_file.write_text(parent_code, encoding="utf-8")
            normalized = diff.replace("a/candidate.py", "a/parent.py").replace("b/candidate.py", "b/parent.py")
            if "a/parent.py" not in normalized and "---" in normalized:
                normalized = re.sub(r"--- a/[^\n]+", "--- a/parent.py", normalized, count=1)
                normalized = re.sub(r"\+\+\+ b/[^\n]+", "+++ b/parent.py", normalized, count=1)
            patch_file = tmp_path / "p.patch"
            patch_file.write_text(normalized, encoding="utf-8")
            result = subprocess.run(
                ["git", "apply", "--whitespace=nowarn", str(patch_file)],
                cwd=str(tmp_path),
                capture_output=True, text=True
            )
            if result.returncode == 0 and parent_file.exists():
                return parent_file.read_text(encoding="utf-8")
            result2 = subprocess.run(
                ["patch", "-p1", "--forward", "-i", str(patch_file)],
                cwd=str(tmp_path),
                capture_output=True, text=True
            )
            if result2.returncode == 0 and parent_file.exists():
                return parent_file.read_text(encoding="utf-8")
    except Exception:
        lines = diff.splitlines()
        hunk_start = None
        for i, l in enumerate(lines):
            if l.startswith("@@"):
                hunk_start = i
                break
        if hunk_start is not None:
            new_lines = []
            for l in lines[hunk_start + 1 :]:
                if l.startswith("+") and not l.startswith("+++"):
                    new_lines.append(l[1:])
                elif l.startswith(" "):
                    new_lines.append(l[1:])
            if new_lines:
                candidate = "\n".join(new_lines)
                if "def " in candidate or "return" in candidate:
                    return candidate
    return diff


class CandidateProcessor:
    """Encapsulates candidate evaluation logic previously in _process_candidate closure."""

    def __init__(
        self,
        *,
        stats: EvolutionStats,
        cache: ContentCache,
        emit: Callable[[str, Dict[str, Any]], None],
        dedup_index: Any,
        funnel: Funnel,
        config: EvolutionConfig,
        hall_of_fame: Any,
        pareto: Any,
        failure: Any,
        budgets: BudgetState,
        ckpt_store: Optional[CheckpointStore],
        ckpt: Optional[Checkpoint],
        bandit: Any,
        evaluator: Callable[[str], Dict[str, float]],
        codes: Dict[str, str],
    ) -> None:
        self.stats = stats
        self.cache = cache
        self.emit = emit
        self.dedup_index = dedup_index
        self.funnel = funnel
        self.config = config
        self.hall_of_fame = hall_of_fame
        self.pareto = pareto
        self.failure = failure
        self.budgets = budgets
        self.ckpt_store = ckpt_store
        self.ckpt = ckpt
        self.bandit = bandit
        self.evaluator = evaluator
        self.codes = codes
        self._seen_ctx: Dict[str, Any] = {"_seen": set()}

    async def process(self, resp: Any, op_name: str, parent_code: str, parent_id: str, gen: int) -> None:
        s = self.stats
        candidate_code = (resp.content or "").strip()
        if not candidate_code:
            s.invalid += 1
            return
        if "```" in candidate_code:
            blocks = re.findall(r"```(?:python|diff|py)?\s*\n(.*?)\n```", candidate_code, re.DOTALL)
            if blocks:
                for b in blocks:
                    if "def " in b or "class " in b or "---" in b or "diff --git" in b:
                        candidate_code = b.strip()
                        break
                else:
                    candidate_code = blocks[0].strip()

        is_diff = "diff --git" in candidate_code or "--- a/" in candidate_code
        original_candidate = candidate_code
        if is_diff:
            candidate_code = _apply_diff(parent_code, original_candidate)
            candidate_id = _hash_code(candidate_code)
            ck = self.cache.make_key(patch=original_candidate)
            if self.cache.get(ck) is not None:
                s.duplicates += 1
                s.cache_hits += 1
                self.emit("gate", {"stage": "g1_dedup", "passed": False, "reason": "exact diff dedup (cache)"})
                return
            funnel_results = self.funnel.run(candidate_code, self._seen_ctx)
            if not self.funnel.passed(funnel_results):
                failed = self.funnel.failed_at(funnel_results)
                self.emit("gate", {"stage": str(failed), "passed": False, "reason": funnel_results[-1].reason})
                if failed == Stage.G1_DEDUP:
                    s.duplicates += 1
                else:
                    s.invalid += 1
                self.cache.put(ck, {"valid": False})
                return
        else:
            candidate_id = _hash_code(candidate_code)
            ck = self.cache.make_key(patch=candidate_code)
            cached = self.cache.get(ck)
            if cached is not None:
                s.duplicates += 1
                s.cache_hits += 1
                self.emit("gate", {"stage": "g1_dedup", "passed": False, "reason": "cache hit"})
                return
            funnel_results = self.funnel.run(candidate_code, self._seen_ctx)
            if not self.funnel.passed(funnel_results):
                failed = self.funnel.failed_at(funnel_results)
                self.emit("gate", {"stage": str(failed), "passed": False, "reason": funnel_results[-1].reason})
                if failed == Stage.G1_DEDUP:
                    s.duplicates += 1
                else:
                    s.invalid += 1
                self.cache.put(ck, {"valid": False})
                return

        if self.dedup_index is not None:
            try:
                self.dedup_index.add(candidate_code, candidate_id, use_structural=False)
            except Exception:
                pass

        s.candidates += 1

        eval_t0 = time.time()
        try:
            if self.config.repo_root and Path(self.config.repo_root).exists():
                with isolated_worktree(Path(self.config.repo_root)) as wt:
                    (wt / "candidate.py").write_text(candidate_code, encoding="utf-8")
                    metrics = self.evaluator(candidate_code)
            else:
                metrics = self.evaluator(candidate_code)
        except Exception as e:
            metrics = {"score": 0.0, "error": str(e)[:200]}
            self.emit("eval_error", {"candidate": candidate_id, "error": str(e)})

        eval_wall = time.time() - eval_t0
        score = float(metrics.get("combined_score", metrics.get("score", 0.0)))
        is_improvement = score > s.best_score

        self.emit("eval", {"candidate": candidate_id, "score": score, "metrics": metrics, "wall_s": round(eval_wall, 3), "improvement": is_improvement})

        self.cache.put(ck, {"score": score, "metrics": metrics, "valid": True})

        admitted = False
        if self.hall_of_fame is not None:
            try:
                from oe_max.archives import Entry as ArchiveEntry
                entry = ArchiveEntry(candidate_id=candidate_id, metrics={"combined_score": score}, generation=gen, operator=op_name)
                admitted = self.hall_of_fame.consider(entry)
                self.emit("archive", {"hall_of_fame": admitted, "pareto": False})
            except Exception:
                pass
        if self.pareto is not None:
            try:
                from oe_max.archives import Entry as ArchiveEntry
                entry = ArchiveEntry(candidate_id=candidate_id, metrics={"score": score})
                self.pareto.consider(entry)
            except Exception:
                pass

        if is_improvement:
            s.best_score = score
            s.best_code = candidate_code
            s.improvements += 1
            self.emit("improvement", {"candidate": candidate_id, "score": score, "generation": gen + 1})

        s.valid += 1
        self.budgets.candidates_evaluated += 1
        self.codes[candidate_id] = candidate_code

        if self.ckpt_store and self.ckpt:
            self.ckpt.generation = gen + 1
            self.ckpt.candidates.append({"id": candidate_id, "score": score, "parent": parent_id})
            self.ckpt.metrics = {"best_score": s.best_score, "operator_stats": s.operator_stats, "dedup": self.dedup_index.stats() if self.dedup_index else {}}
            self.ckpt.budgets = self.budgets.to_dict()
            self.ckpt_store.save(self.ckpt)
            try:
                self.cache.persist()
            except Exception:
                pass
            if self.dedup_index is not None and self.config.checkpoint_dir:
                try:
                    dpath = Path(self.config.checkpoint_dir) / "dedup.json"
                    dpath.parent.mkdir(parents=True, exist_ok=True)
                    _tmp = dpath.with_suffix(".tmp")
                    _tmp.write_text(json.dumps({"hits": self.dedup_index.hits, "stats": self.dedup_index.stats()}, indent=2), encoding="utf-8")
                    _tmp.replace(dpath)
                except Exception:
                    pass

        s.operator_stats.setdefault(op_name, {"valid": 0, "novel": 0, "archived": 0, "improve": 0, "total": 0, "pareto": 0, "regression": 0})
        s.operator_stats[op_name]["total"] += 1
        s.operator_stats[op_name]["valid"] += 1
        s.operator_stats[op_name]["novel"] += 1
        if self.hall_of_fame is not None and admitted:
            s.operator_stats[op_name]["archived"] += 1
        if is_improvement:
            s.operator_stats[op_name]["improve"] += 1
        elif score < s.best_score - 0.1:
            s.operator_stats[op_name]["regression"] += 1

        if self.bandit is not None:
            try:
                valid_r = 1.0
                improve_r = 1.0 if is_improvement else 0.0
                archive_r = 0.5 if admitted else 0.0
                delta = max(0.0, min(1.0, (score - (s.best_score - (1.0 if is_improvement else 0.0))) / 2.0))
                pareto_r = 0.3 if is_improvement else 0.0
                eff_r = min(1.0, 1.0 / max(0.1, eval_wall))
                token_r = 0.5
                reg_penalty = -0.4 if s.operator_stats[op_name]["regression"] > 0 else 0.0
                reward = max(0.0, min(1.0, 0.25 * valid_r + 0.30 * improve_r + 0.15 * archive_r + 0.10 * delta + 0.07 * pareto_r + 0.05 * eff_r + 0.05 * token_r + 0.03 * reg_penalty))
                self.bandit.update(op_name, reward)
                self.emit("bandit", {"operator": op_name, "reward": round(reward, 3), "snapshot": self.bandit.snapshot()})
            except Exception as e:
                self.emit("bandit_error", {"operator": op_name, "error": str(e)})

        self.emit("generation_done", {
            "generation": gen + 1,
            "candidate": candidate_id,
            "parent": parent_id,
            "operator": op_name,
            "gate": "pass",
            "score": score,
            "delta": round(score - s.best_score, 4) if s.best_score != float("-inf") else 0,
            "best_score": s.best_score,
            "cache_hit": False,
        })

    def _record_brain_failure(
        self,
        gen: int,
        op_name: str,
        error: str,
        candidate_id: str,
        *,
        emit_fn: Callable[[str, Dict[str, Any]], None],
        emit_event: str = "brain_error",
        emit_extra: Optional[Dict[str, Any]] = None,
        stats: "EvolutionStats",
        budgets: Any,
        failure: Any,
        bandit: Any,
    ):
        emit_data: Dict[str, Any] = {"generation": gen, "error": error}
        if emit_extra:
            emit_data.update(emit_extra)
        if "operator" not in emit_data:
            emit_data["operator"] = op_name
        emit_fn(emit_event, emit_data)
        stats.candidates += 1
        budgets.failures += 1
        if failure is not None:
            try:
                from oe_max.archives import Entry as ArchiveEntry
                failure.consider(ArchiveEntry(candidate_id=candidate_id, note=error[:200], operator=op_name))
            except Exception:
                pass
        if bandit is not None:
            try:
                bandit.update(op_name, 0.0)
            except Exception:
                pass


async def run_evolution(
    brain: BrainPort,
    config: EvolutionConfig,
    *,
    on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    evaluator: Optional[Callable[[str], Dict[str, float]]] = None,
) -> EvolutionStats:
    """
    Minimal but functional evolution loop.

    - Parent selection: random from archive (or initial)
    - Operator selection: uniform or via existing bandit if available
    - BrainRequest via BrainPort (policy = mutation-generation)
    - Funnel: G0 -> G1 (exact/normalized via cache) -> evaluator (isolated if repo_root)
    - Archives + checkpoint
    """
    t0 = time.time()
    random.seed(config.seed)

    def emit(event: str, data: Dict[str, Any]):
        if on_event:
            try:
                on_event(event, data)
            except Exception:
                pass

    cache_path = None
    if config.checkpoint_dir:
        try:
            cache_path = Path(config.checkpoint_dir) / "content_cache.json"
        except Exception:
            cache_path = None
    cache = ContentCache(max_entries=4096, persist_path=cache_path)
    budgets = BudgetState(config=config.budgets, started_at=t0)
    stats = EvolutionStats()
    stats.best_code = config.initial_code

    # Archives
    hall_of_fame = None
    pareto = None
    failure = None
    if HAS_ARCHIVES:
        try:
            hall_of_fame = HallOfFame()
            pareto = ParetoArchive()
            failure = FailureArchive()
        except Exception:
            pass

    # Funnel + persistent dedup index (exact/normalized/AST/structural)
    funnel = Funnel(FunnelConfig(halving_keep_ratio=config.halving_ratio))
    dedup_index = None
    if HAS_GATES:
        from oe_max.evaluation.gates import DedupIndex
        dedup_index = DedupIndex()
        funnel.register(Stage.G0_VALIDITY, lambda code, ctx: _wrap_g0(code))
        def g1_fn(code: str, ctx: Dict[str, Any]) -> FunnelResult:
            r = dedup_index.check(code, use_structural=False)
            if r.passed:
                return FunnelResult(passed=True, stage=Stage.G1_DEDUP)
            return FunnelResult(passed=False, stage=Stage.G1_DEDUP, reason=r.reason, detail=r.detail)
        funnel.register(Stage.G1_DEDUP, g1_fn)
    else:
        def g1_fn(code: str, ctx: Dict[str, Any]) -> FunnelResult:
            h = _hash_code(code)
            if h in ctx.get("_seen", set()):
                return FunnelResult(passed=False, stage=Stage.G1_DEDUP, reason="exact dedup (funnel)")
            ctx.setdefault("_seen", set()).add(h)
            return FunnelResult(passed=True, stage=Stage.G1_DEDUP)
        funnel.register(Stage.G1_DEDUP, g1_fn)

    seen_ctx: Dict[str, Any] = {"_seen": set()}
    # Persistent dedup state load if checkpoint exists
    if dedup_index is not None and config.checkpoint_dir:
        try:
            state_path = Path(config.checkpoint_dir) / "dedup.json"
            if state_path.exists():
                d = json.loads(state_path.read_text(encoding="utf-8"))
                # Rehydrate hits counts; hashes themselves are content-addressed so we can reload
                dedup_index.hits.update(d.get("hits", {}))
        except Exception:
            pass

    # Checkpoint
    ckpt_store = None
    ckpt = None
    if config.checkpoint_dir:
        ckpt_store = CheckpointStore(Path(config.checkpoint_dir))
        ckpt = Checkpoint(goal=f"evolution:{config.iterations}", seed=config.seed)
        ckpt_store.save(ckpt)

    # Default evaluator: returns random fitness if none provided
    if evaluator is None:
        def default_eval(code: str) -> Dict[str, float]:
            # Cheap deterministic fitness: count non-empty lines + random
            lines = [l for l in code.splitlines() if l.strip()]
            return {"score": float(len(lines)) + random.random(), "combined_score": float(len(lines))}
        evaluator = default_eval

    codes: Dict[str, str] = {"gen-0": config.initial_code}

    # Bandit — richer reward than just fitness delta
    bandit = None
    if config.use_bandit and HAS_OE_MAX:
        try:
            from oe_max.search.bandit import DiscountedThompsonSampling
            arms = list(OPERATORS.keys()) if OPERATORS else ["LOCAL_OPTIMIZE"]
            arm_names = [a.value if hasattr(a, "value") else str(a) for a in arms]
            bandit = DiscountedThompsonSampling(arm_names, gamma=0.95, seed=config.seed)
            bandit._arm_map = {n: n for n in arm_names}
        except Exception:
            bandit = None

    # Backoff for transient brain failures
    backoff = GenericBackoff(base_s=1.0, max_s=30.0)

    processor = CandidateProcessor(
        stats=stats,
        cache=cache,
        emit=emit,
        dedup_index=dedup_index,
        funnel=funnel,
        config=config,
        hall_of_fame=hall_of_fame,
        pareto=pareto,
        failure=failure,
        budgets=budgets,
        ckpt_store=ckpt_store,
        ckpt=ckpt,
        bandit=bandit,
        evaluator=evaluator,
        codes=codes,
    )


    # Cache operator keys and config lookups outside generation loop
    operator_keys_cache = list(OPERATORS.keys()) if OPERATORS else []
    config_batch_size = getattr(config, "batch_size", 1) or 1
    config_max_inflight = getattr(config.budgets, "max_brain_inflight", 4) or 4
    sem = asyncio.Semaphore(config_max_inflight)

    for gen in range(config.iterations):
        if budgets.exhausted():
            emit("budget_exhausted", {"reason": budgets.exhausted(), "generation": gen})
            break

        stats.generation = gen + 1
        budgets.generations = gen + 1

        # Parent selection: novelty-aware + HallOfFame, not just best
        parent_id = f"gen-{gen}"
        parent_code = codes.get(parent_id, stats.best_code)
        # 30% chance to pick a diverse parent from HallOfFame/Pareto/novelty, else best
        if hall_of_fame and hall_of_fame.entries and random.random() < 0.3:
            try:
                # Prefer recent HallOfFame entries for diversity, or Pareto front
                if pareto and pareto.front() and random.random() < 0.5:
                    # Pick from Pareto front
                    front = pareto.front()
                    if front:
                        pick = random.choice(front)
                        parent_id = pick.candidate_id
                        parent_code = codes.get(parent_id, stats.best_code)
                else:
                    # Pick random HallOfFame entry (not necessarily best)
                    hof_entries = hall_of_fame.entries
                    if hof_entries:
                        pick = random.choice(hof_entries)
                        parent_id = pick.candidate_id
                        parent_code = codes.get(parent_id, stats.best_code)
            except Exception:
                pass

        # Batch: generate batch_size candidates concurrently, bounded by max_brain_inflight
        batch = max(1, config_batch_size)
        inflight = config_max_inflight
        slot_ops: List[str] = []
        for _slot in range(batch):
            op_name = "LOCAL_OPTIMIZE"
            if bandit is not None:
                try:
                    op_name = bandit.select()
                except Exception:
                    op_name = "LOCAL_OPTIMIZE"
            elif HAS_OE_MAX and operator_keys_cache:
                try:
                    pick_key = random.choice(operator_keys_cache)
                    op_name = pick_key.value if hasattr(pick_key, "value") else str(pick_key)
                except Exception:
                    op_name = "LOCAL_OPTIMIZE"
            slot_ops.append(op_name)

        emit("generation", {"generation": gen + 1, "parent": parent_id, "operator": slot_ops[0], "batch": batch, "best_score": stats.best_score, "bandit": bandit.snapshot() if bandit else None})

        async def _gen_one(op_name_slot: str) -> Any:
            failure_context_local: List[str] = []
            if failure is not None:
                try:
                    failure_context_local = failure.recent_for_prompt(limit=2, operator=op_name_slot)
                    if not failure_context_local:
                        failure_context_local = failure.recent_for_prompt(limit=2)
                except Exception:
                    failure_context_local = []
            ctx = {
                "generation": gen,
                "best_score": round(stats.best_score, 4) if stats.best_score != float("-inf") else None,
                "operator": op_name_slot,
                "attempt": stats.candidates,
            }
            if failure_context_local:
                ctx["recent_failures"] = failure_context_local[:2]
            req = BrainRequest(
                operation=Operation.PATCH if config.use_patch else Operation.MUTATE,
                objective=f"Improve the program. Current best score {ctx['best_score']}. Propose a SMALL targeted patch (unified diff) over the parent. Keep changes minimal.",
                parent_code=parent_code,
                parent_id=parent_id,
                mutation_strategy=op_name_slot,
                policy=PolicyMode.MUTATION_GENERATION,
                context=ctx,
                extra={"temperature": 0.7},
            )
            async with sem:
                try:
                    resp = await brain.generate(req)
                    return ("ok", op_name_slot, resp)
                except Exception as e:
                    return ("error", op_name_slot, e)

        results = await asyncio.gather(*[_gen_one(opn) for opn in slot_ops], return_exceptions=True)

        for item in results:
            if isinstance(item, Exception):
                processor._record_brain_failure(
                    gen, "LOCAL_OPTIMIZE", str(item), f"fail-{gen}-task",
                    emit_fn=emit, emit_extra={"trace": traceback.format_exc()},
                    stats=stats, budgets=budgets, failure=failure, bandit=bandit,
                )
                continue
            kind, op_name, payload = item
            if kind == "error":
                processor._record_brain_failure(
                    gen, op_name, str(payload), f"fail-{gen}",
                    emit_fn=emit, emit_extra={"trace": traceback.format_exc()},
                    stats=stats, budgets=budgets, failure=failure, bandit=bandit,
                )
                continue
            resp = payload
            stats.brain_calls += 1
            backoff.reset()
            if not resp.ok:
                processor._record_brain_failure(
                    gen, op_name, resp.error or "", f"fail-{gen}",
                    emit_fn=emit, emit_event="brain_failure", emit_extra={"error": resp.error or ""},
                    stats=stats, budgets=budgets, failure=failure, bandit=bandit,
                )
                continue
            await processor.process(resp, op_name, parent_code, parent_id, gen)

    stats.wall_s = time.time() - t0
    stats.cache_hits = cache.hits
    emit("done", {"stats": stats.__dict__, "best_code": stats.best_code[:500]})
    return stats


def _wrap_g0(code: str):
    from oe_max.evaluation.gates import g0_validity as _g0
    r = _g0(code)
    if r.passed:
        return FunnelResult(passed=True, stage=Stage.G0_VALIDITY)
    return FunnelResult(passed=False, stage=Stage.G0_VALIDITY, reason=r.reason)


def _load_evaluator(path: str):
    """Load evaluator function from a Python file (expects `evaluate` or `evaluate_function`)."""
    spec = importlib.util.spec_from_file_location("evaluator_mod", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore
    for name in ("evaluate", "evaluate_function", "run_evaluation"):
        if hasattr(mod, name):
            fn = getattr(mod, name)
            # Wrap to handle program_path vs code string
            def _wrap(code: str, _fn=fn):
                # Try code string first; fallback to temp file
                try:
                    return _fn(code)
                except TypeError:
                    # Assume expects file path
                    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as f:
                        f.write(code)
                        fpath = f.name
                    try:
                        return _fn(fpath)
                    finally:
                        try:
                            pathlib.Path(fpath).unlink()
                        except Exception:
                            pass
                except Exception as e:
                    return {"score": 0.0, "error": str(e)}
            return _wrap
    return None


async def _cli_main():
    import argparse
    parser = argparse.ArgumentParser(description="Alpha Evolve — provider-neutral evolution (BrainPort)")
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--initial", type=str, default=None, help="path to initial program")
    parser.add_argument("--evaluator", type=str, default=None, help="path to evaluator.py")
    parser.add_argument("--output", type=str, default="evolution_output")
    parser.add_argument("--brain", choices=["null", "legacy"], default="null")
    parser.add_argument("--repo", type=str, default=None, help="repo root for isolation")
    args = parser.parse_args()

    initial_code = "def solve(x):\n    return x\n"
    if args.initial and Path(args.initial).exists():
        initial_code = Path(args.initial).read_text(encoding="utf-8")

    evaluator = None
    if args.evaluator and Path(args.evaluator).exists():
        evaluator = _load_evaluator(args.evaluator)
        if evaluator:
            print(f"Loaded evaluator from {args.evaluator}")

    from .port import NullBrainPort
    from .legacy_adapter import LegacyBrainPort as _Legacy
    brain = _Legacy() if args.brain == "legacy" else NullBrainPort()

    cfg = EvolutionConfig(
        iterations=args.iterations,
        seed=args.seed,
        initial_code=initial_code,
        checkpoint_dir=Path(args.output) / "checkpoints",
        repo_root=Path(args.repo) if args.repo else None,
    )

    def on_event(ev, data):
        if ev in ("generation_done", "improvement", "gate", "eval"):
            print(f"[{ev}] {data}")

    stats = await run_evolution(brain, cfg, on_event=on_event, evaluator=evaluator)
    print(f"\nDone: gen={stats.generation} cands={stats.candidates} valid={stats.valid} dup={stats.duplicates} best={stats.best_score:.4f} wall={stats.wall_s:.1f}s")
    # Persist best
    out = _P(args.output)
    out.mkdir(parents=True, exist_ok=True)
    (out / "best.py").write_text(stats.best_code, encoding="utf-8")
    (out / "stats.json").write_text(json.dumps(stats.__dict__, indent=2), encoding="utf-8")
    print(f"Best written to {out / 'best.py'}")


def main():
    import asyncio as _aio
    _aio.run(_cli_main())

if __name__ == "__main__":
    main()
