"""
Engine instrumentation.

Design choice that shapes this whole file: we instrument by *wrapping public
methods at runtime* and observing real engine state before/after the call —
not by editing OpenEvolve's source.

Why: the fork must keep merging upstream (SOURCE_OF_TRUTH section 27). Editing
call sites across database.py / controller.py / evaluator.py would create a
patch surface that conflicts on every upstream release. Wrapping keeps
PATCH_SURFACE.md empty for telemetry, so an upstream merge is a fast-forward.

The cost is that we depend on method *names* rather than internals. That is the
cheaper dependency: `ProgramDatabase.add` is public API and stable, whereas the
internals of MAP-Elites placement change often. Every hook degrades to a no-op
with a warning if the symbol it wants is missing, so a rename downgrades
telemetry rather than breaking evolution.

State is read back from the live objects (`island_feature_maps`, `programs`,
`best_program_id`) after the real call, so every emitted value is measured, not
predicted.
"""

from __future__ import annotations

import contextvars
import functools
import hashlib
import logging
import os
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .bus import configure_bus, emit, get_bus
from .events import Component, Event, EventType, Status, new_id

logger = logging.getLogger(__name__)

# Links a candidate back to the model request that generated it.
#
# Upstream never attaches a candidate id to the generating call — measured: 0 of
# 22 stored model requests carried one — so no post-hoc join can recover the
# attribution. It has to be captured at generation time.
#
# A ContextVar rather than a global because OpenEvolve runs iterations as
# concurrent asyncio tasks (`parallel_evaluations`). ContextVars are per-task
# and are copied into each new task, so two iterations in flight cannot read
# each other's request. A module-level global would silently mis-attribute
# under exactly the concurrency the engine uses by default.
_generating_request: contextvars.ContextVar = contextvars.ContextVar(
    "evolution_generating_request", default=None
)

# The key under which a worker process stamps the attribution onto the child
# program's metadata.
#
# A ContextVar alone is not enough, and the reason is structural rather than
# incidental. In `process_parallel` the LLM call and the `Program` construction
# happen in a *worker process*; `ProgramDatabase.add` happens in the *main*
# process, which receives only a pickled `SerializableResult`. No in-memory
# mechanism — ContextVar, thread-local or global — crosses that boundary, and
# measuring it is what exposed it: a live run produced 3 candidates and 0
# attributed ones while the ContextVar was set correctly in every worker.
#
# `Program.metadata` is the one channel that does cross: it is a dataclass
# field, so it survives `to_dict()` → pickle → `Program(**dict)`. Upstream reads
# metadata by key only (`island`, `migrant`, `changes`, `parent_metrics`) and
# never renders it wholesale into a prompt, so an extra namespaced key is inert.
ATTRIBUTION_KEY = "evolution_generation"

# Process-local record of the model request that generated the candidate this
# worker is currently building. First write wins within an iteration, because
# the *generating* call is the first LLM call of the iteration and any later
# call is the evaluator's LLM feedback — crediting a mutation to the request
# that judged it would be worse than not crediting it at all.
_worker_generation: Optional[Dict[str, Any]] = None
_worker_generation_pid: Optional[int] = None


def _begin_worker_attribution() -> None:
    """Start a fresh attribution window. Called once per worker iteration."""
    global _worker_generation, _worker_generation_pid
    _worker_generation = None
    _worker_generation_pid = os.getpid()


def _publish_generation(record: Dict[str, Any]) -> None:
    """
    Record a completed model request for the candidate it will produce.

    Both channels are written: the ContextVar for the in-process path (where
    generation and `add` share a task context) and the process-local slot for
    the worker path (where they do not share a process at all).
    """
    global _worker_generation
    try:
        _generating_request.set(record)
    except Exception:  # pragma: no cover - ContextVar.set does not normally fail
        pass
    if _worker_generation is None and _worker_generation_pid == os.getpid():
        _worker_generation = record


def _broker_route(response: Any) -> Optional[Dict[str, Any]]:
    """
    Which provider and model actually served a request that went through the
    OE-MAX broker.

    The engine only ever names the broker alias (`oe-max-primary`), so without
    this every route collapses into one row called "local/oe-max-primary" and
    a route comparison becomes impossible in exactly the configuration the
    project ships. The broker already stamps `body["oe_max"]` on every
    response — this reads it back off the parsed object, where the OpenAI
    client keeps unknown fields in `model_extra`.

    Returns None for any endpoint that is not the broker, which is why this is
    additive: a direct provider call is unaffected.
    """
    for get in (lambda: getattr(response, "oe_max", None),
                lambda: (getattr(response, "model_extra", None) or {}).get("oe_max"),
                lambda: response["oe_max"] if isinstance(response, dict) else None):
        try:
            value = get()
        except Exception:
            continue
        if isinstance(value, dict) and value.get("model"):
            return value
    return None


def _generation_role() -> str:
    """
    Whether this request is the one *generating* a mutation.

    Upstream builds a second LLM ensemble for evaluator feedback
    (`use_llm_feedback`, off by default). Both ensembles go through the same
    `OpenAILLM.generate_with_context`, so without this they would be
    indistinguishable in the event log — and the analysis would charge a route
    for an "attempt" that was really the evaluator grading someone else's work.

    The generating call is the first of a worker iteration, by construction:
    evaluation cannot run before there is something to evaluate.
    """
    env = os.environ.get("EVOLUTION_LLM_ROLE")
    if env:
        return env
    if _worker_generation_pid == os.getpid() and _worker_generation is not None:
        return "evaluation"
    return "mutation"


def _take_worker_attribution() -> Optional[Dict[str, Any]]:
    """Consume this iteration's attribution, if a request was made."""
    global _worker_generation
    rec, _worker_generation = _worker_generation, None
    return rec


def _attribution_of(program: Any) -> Optional[Dict[str, Any]]:
    """
    Which model request produced this candidate, or None if it cannot be known.

    Order matters: the metadata stamp is exact (the worker that made the call
    also built the program), so it wins over the ContextVar, which is only
    correct when generation and `add` share a task.

    Two cases are deliberately left unattributed rather than guessed at:

    * a **migrant** — a copy of an already-attributed program. Crediting the
      route a second time would inflate its attempt count and its measured
      yield, which is precisely the number `route_quality` exists to compare.
    * a **stale** ContextVar — a candidate added long after the last request in
      this context is a checkpoint reload or a copy, not that request's output.
    """
    md = getattr(program, "metadata", None) or {}
    if md.get("migrant"):
        return None
    rec = md.get(ATTRIBUTION_KEY)
    if isinstance(rec, dict) and rec.get("request_id"):
        return rec
    rec = _generating_request.get()
    if rec and (time.time() - rec.get("at", 0)) > _ATTRIBUTION_MAX_AGE_S:
        return None
    return rec


def _attribution_fields(rec: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The provenance block attached to every candidate event."""
    rec = rec or {}
    return {
        "generating_request_id": rec.get("request_id"),
        "generating_provider": rec.get("provider"),
        "generating_model": rec.get("model"),
        "generating_latency_ms": rec.get("latency_ms"),
        "generating_tokens": rec.get("tokens"),
    }

# Env keys used to propagate telemetry config into worker processes.
ENV_RUN_ID = "EVOLUTION_RUN_ID"
ENV_EXPERIMENT_ID = "EVOLUTION_EXPERIMENT_ID"
ENV_EVENT_LOG = "EVOLUTION_EVENT_LOG"
ENV_COLLECTOR_PORT = "EVOLUTION_COLLECTOR_PORT"
ENV_ENABLED = "EVOLUTION_TELEMETRY"


# A candidate added more than this long after the context's last model request
# is not attributed to it. Generous, because a slow evaluator legitimately sits
# between the two, but bounded so migrants and reloads are not misattributed.
_ATTRIBUTION_MAX_AGE_S = 900.0


def _fitness(metrics: Dict[str, float], feature_dims: Optional[List[str]] = None) -> Optional[float]:
    """
    Use upstream's own fitness definition so our number always equals the number
    the engine selected on. Re-deriving it here would let the UI drift from the
    engine, which is exactly the class of bug section 36 forbids.
    """
    try:
        from openevolve.utils.metrics_utils import get_fitness_score

        return float(get_fitness_score(metrics or {}, feature_dims or []))
    except Exception:
        if not metrics:
            return None
        if "combined_score" in metrics:
            try:
                return float(metrics["combined_score"])
            except (TypeError, ValueError):
                return None
        nums = [v for v in metrics.values() if isinstance(v, (int, float))]
        return float(sum(nums) / len(nums)) if nums else None


def _hash(text: Optional[str]) -> Optional[str]:
    return hashlib.sha256(text.encode()).hexdigest()[:16] if text else None


class EngineInstrumentation:
    """Installs/removes runtime hooks on the OpenEvolve engine."""

    def __init__(
        self,
        run_id: str,
        experiment_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        capture_code: bool = True,
    ) -> None:
        self.run_id = run_id
        self.experiment_id = experiment_id or run_id
        self.trace_id = trace_id or run_id
        self.capture_code = capture_code
        self._patches: List[Tuple[Any, str, Any]] = []
        self._installed = False
        # Island membership snapshot, used to derive real migration edges.
        self._island_snapshot: Dict[int, Set[str]] = {}

    # -- helpers -------------------------------------------------------

    def _ev(self, type_: EventType, component: Component, **kw: Any) -> Event:
        kw.setdefault("run_id", self.run_id)
        kw.setdefault("experiment_id", self.experiment_id)
        kw.setdefault("trace_id", self.trace_id)
        return Event(type=type_, component=component, **kw)

    def _patch(self, target: Any, name: str, factory: Callable[[Any], Any]) -> bool:
        original = getattr(target, name, None)
        if original is None:
            logger.warning(
                "Evolution telemetry: %s.%s missing; that hook is disabled "
                "(upstream may have renamed it).", getattr(target, "__name__", target), name
            )
            return False
        if getattr(original, "__evolution_instrumented__", False):
            # Already wrapped — most often because this process was forked from
            # one that had hooks installed. Wrapping again would emit every
            # event twice and inflate every count in the UI.
            return False
        wrapper = factory(original)
        functools.update_wrapper(wrapper, original)
        wrapper.__evolution_instrumented__ = True  # type: ignore[attr-defined]
        setattr(target, name, wrapper)
        self._patches.append((target, name, original))
        return True

    # -- install -------------------------------------------------------

    def install(self) -> "EngineInstrumentation":
        if self._installed:
            return self
        self._install_database()
        self._install_evaluator()
        self._install_llm()
        self._install_controller()
        self._installed = True
        logger.info("Evolution telemetry installed (%d hooks, run=%s)",
                    len(self._patches), self.run_id)
        return self

    def uninstall(self) -> None:
        for target, name, original in reversed(self._patches):
            try:
                setattr(target, name, original)
            except Exception:
                pass
        self._patches.clear()
        self._installed = False

    def __enter__(self) -> "EngineInstrumentation":
        return self.install()

    def __exit__(self, *exc: Any) -> None:
        self.uninstall()

    # -- database ------------------------------------------------------

    def _install_database(self) -> None:
        try:
            from openevolve.database import ProgramDatabase
        except ImportError:
            logger.warning("Evolution telemetry: openevolve.database unavailable")
            return

        inst = self

        def add_factory(original: Callable) -> Callable:
            def wrapper(db_self, program, iteration=None, target_island=None, *a, **kw):
                t0 = time.perf_counter()
                # Snapshot only what we need to diff — cheap, O(cells) of one island.
                before_best = getattr(db_self, "best_program_id", None)
                before_maps = inst._snapshot_feature_maps(db_self)
                before_present = program.id in getattr(db_self, "programs", {})

                result = original(db_self, program, iteration, target_island, *a, **kw)

                dur = (time.perf_counter() - t0) * 1000.0
                try:
                    inst._after_add(
                        db_self, program, iteration, before_best, before_maps,
                        before_present, dur,
                    )
                except Exception as exc:  # telemetry must never break evolution
                    logger.debug("telemetry add hook failed: %r", exc)
                return result

            return wrapper

        self._patch(ProgramDatabase, "add", add_factory)

        def migrate_factory(original: Callable) -> Callable:
            def wrapper(db_self, *a, **kw):
                before = inst._snapshot_islands(db_self)
                gen = getattr(db_self, "last_iteration", None)
                emit(inst._ev(
                    EventType.ISLAND_MIGRATION_STARTED, Component.DATABASE,
                    generation=gen, summary="migration started",
                    metrics={"islands": float(len(before))},
                ))
                t0 = time.perf_counter()
                result = original(db_self, *a, **kw)
                dur = (time.perf_counter() - t0) * 1000.0
                try:
                    inst._after_migration(db_self, before, gen, dur)
                except Exception as exc:
                    logger.debug("telemetry migration hook failed: %r", exc)
                return result

            return wrapper

        self._patch(ProgramDatabase, "migrate_programs", migrate_factory)

        def sample_factory(original: Callable) -> Callable:
            def wrapper(db_self, *a, **kw):
                parent, inspirations = original(db_self, *a, **kw)
                try:
                    emit(inst._ev(
                        EventType.CANDIDATE_SAMPLED, Component.DATABASE,
                        candidate_id=getattr(parent, "id", None),
                        generation=getattr(db_self, "last_iteration", None),
                        island_id=getattr(db_self, "current_island", None),
                        summary=f"sampled parent {getattr(parent, 'id', '?')}",
                        metadata={
                            "inspiration_ids": [getattr(p, "id", None) for p in (inspirations or [])],
                            "num_inspirations": len(inspirations or []),
                        },
                        metrics={"parent_fitness": _fitness(
                            getattr(parent, "metrics", {}),
                            getattr(getattr(db_self, "config", None), "feature_dimensions", None),
                        ) or 0.0},
                    ))
                except Exception as exc:
                    logger.debug("telemetry sample hook failed: %r", exc)
                return parent, inspirations

            return wrapper

        self._patch(ProgramDatabase, "sample", sample_factory)

        def load_factory(original: Callable) -> Callable:
            def wrapper(db_self, path, *a, **kw):
                t0 = time.perf_counter()
                result = original(db_self, path, *a, **kw)
                emit(inst._ev(
                    EventType.CHECKPOINT_LOADED, Component.DATABASE,
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                    summary=f"checkpoint loaded from {path}",
                    metadata={"path": str(path)},
                    metrics={"num_programs": float(len(getattr(db_self, "programs", {}) or {}))},
                ))
                return result

            return wrapper

        self._patch(ProgramDatabase, "load", load_factory)

    def _snapshot_feature_maps(self, db) -> Dict[int, Dict[str, str]]:
        maps = getattr(db, "island_feature_maps", None)
        if not maps:
            return {}
        return {i: dict(m) for i, m in enumerate(maps)}

    def _snapshot_islands(self, db) -> Dict[int, Set[str]]:
        islands = getattr(db, "islands", None) or []
        return {i: set(s) for i, s in enumerate(islands)}

    def _after_add(self, db, program, iteration, before_best, before_maps,
                   before_present, duration_ms) -> None:
        cfg = getattr(db, "config", None)
        dims = list(getattr(cfg, "feature_dimensions", []) or [])
        metrics = dict(getattr(program, "metrics", {}) or {})
        fitness = _fitness(metrics, dims)
        island_id = None
        after_maps = self._snapshot_feature_maps(db)

        # Which island actually holds it now — read, not guessed.
        for idx, members in self._snapshot_islands(db).items():
            if program.id in members:
                island_id = idx
                break

        was_added = program.id in getattr(db, "programs", {})
        rejected = not was_added and not before_present

        code = getattr(program, "code", None)
        payload_metrics = {k: float(v) for k, v in metrics.items()
                           if isinstance(v, (int, float, bool))}
        if fitness is not None:
            payload_metrics["combined_score"] = fitness

        if rejected:
            emit(self._ev(
                EventType.CANDIDATE_REJECTED, Component.DATABASE,
                candidate_id=program.id, generation=getattr(program, "generation", None),
                iteration=iteration, island_id=island_id, status=Status.REJECTED,
                duration_ms=duration_ms,
                summary=f"candidate {program.id} rejected (novelty/duplicate gate)",
                metrics=payload_metrics,
                metadata={"parent_id": getattr(program, "parent_id", None),
                          "reason": "novelty_or_duplicate",
                          # A route whose mutations get rejected must be charged
                          # for them, or duplicate-heavy routes look free.
                          **_attribution_fields(_attribution_of(program))},
            ))
            return

        # Attribute this candidate to the model request that generated it, if
        # one is knowable. See _attribution_of for what is deliberately left
        # unattributed instead of guessed.
        gen_req = _attribution_of(program)

        emit(self._ev(
            EventType.CANDIDATE_CREATED, Component.DATABASE,
            candidate_id=program.id,
            parent_candidate_ids=[p] if (p := getattr(program, "parent_id", None)) else [],
            generation=getattr(program, "generation", None),
            iteration=iteration if iteration is not None else getattr(program, "iteration_found", None),
            island_id=island_id, duration_ms=duration_ms,
            summary=f"candidate {program.id} added",
            metrics=payload_metrics,
            output={"code": code} if self.capture_code else {},
            metadata={
                "parent_id": getattr(program, "parent_id", None),
                "language": getattr(program, "language", None),
                "complexity": getattr(program, "complexity", None),
                "diversity": getattr(program, "diversity", None),
                "changes_summary": getattr(program, "changes_description", None),
                "code_hash": _hash(code),
                "code_length": len(code) if code else None,
                "candidate_type": "code",
                # Generation provenance — the link upstream does not provide.
                **_attribution_fields(gen_req),
            },
        ))

        # MAP-Elites: diff the island's real feature map.
        for idx, after in after_maps.items():
            before = before_maps.get(idx, {})
            for cell_key, occupant in after.items():
                if occupant != program.id:
                    continue
                prev = before.get(cell_key)
                if prev == program.id:
                    continue
                coords = [int(c) for c in cell_key.split("-") if c.lstrip("-").isdigit()]
                replaced = prev is not None
                emit(self._ev(
                    EventType.MAP_ELITES_ELITE_REPLACED if replaced
                    else EventType.MAP_ELITES_CELL_UPDATED,
                    Component.DATABASE,
                    candidate_id=program.id, island_id=idx,
                    generation=getattr(program, "generation", None), iteration=iteration,
                    summary=(f"cell {cell_key} " + ("elite replaced" if replaced else "newly occupied")),
                    metrics={"score": fitness or 0.0},
                    metadata={
                        "cell_key": cell_key, "coords": coords, "dimensions": dims,
                        "previous_candidate_id": prev,
                        "occupied_cells": len(after),
                    },
                ))

        # Best-program transition, read from the engine's own tracker.
        after_best = getattr(db, "best_program_id", None)
        if after_best and after_best != before_best and after_best == program.id:
            emit(self._ev(
                EventType.CANDIDATE_BEST_UPDATED, Component.DATABASE,
                candidate_id=program.id, generation=getattr(program, "generation", None),
                iteration=iteration, island_id=island_id,
                summary=f"new best {program.id}",
                metrics=payload_metrics,
                metadata={"previous_best_id": before_best},
            ))

        # Island + archive rollup.
        if island_id is not None:
            self._emit_island_update(db, island_id, iteration)
        archive = getattr(db, "archive", None)
        if archive is not None:
            emit(self._ev(
                EventType.ARCHIVE_UPDATED, Component.DATABASE,
                iteration=iteration, summary="archive updated",
                metrics={"archive_size": float(len(archive)),
                         "population": float(len(getattr(db, "programs", {}) or {}))},
            ))

    def _emit_island_update(self, db, island_id: int, iteration) -> None:
        islands = getattr(db, "islands", None) or []
        if island_id >= len(islands):
            return
        members = list(islands[island_id])
        programs = getattr(db, "programs", {}) or {}
        cfg = getattr(db, "config", None)
        dims = list(getattr(cfg, "feature_dimensions", []) or [])
        scores = sorted(
            s for s in (
                _fitness(getattr(programs[pid], "metrics", {}), dims)
                for pid in members if pid in programs
            ) if s is not None
        )
        median = scores[len(scores) // 2] if scores else None
        gens = getattr(db, "island_generations", None) or []
        best_ids = getattr(db, "island_best_programs", None) or []
        metrics: Dict[str, float] = {"population": float(len(members))}
        if scores:
            metrics["best_score"] = scores[-1]
            metrics["median_score"] = float(median)
        maps = getattr(db, "island_feature_maps", None) or []
        if island_id < len(maps):
            metrics["occupied_cells"] = float(len(maps[island_id]))
        # Diversity via the engine's own calculation, so the Islands table shows
        # the same number the engine logs rather than a second definition of it.
        calc = getattr(db, "_calculate_island_diversity", None)
        if callable(calc):
            try:
                island_programs = [programs[pid] for pid in members if pid in programs]
                if island_programs:
                    metrics["diversity"] = float(calc(island_programs))
            except Exception:
                # Diversity is a nice-to-have; never let it break the rollup.
                pass
        emit(self._ev(
            EventType.ISLAND_UPDATED, Component.DATABASE,
            island_id=island_id, iteration=iteration,
            generation=gens[island_id] if island_id < len(gens) else None,
            summary=f"island {island_id} updated", metrics=metrics,
            metadata={"best_candidate_id": best_ids[island_id]
                      if island_id < len(best_ids) else None},
        ))

    def _after_migration(self, db, before: Dict[int, Set[str]], generation, duration_ms) -> None:
        after = self._snapshot_islands(db)
        migrations: List[Dict[str, Any]] = []
        programs = getattr(db, "programs", {}) or {}
        for idx, members in after.items():
            for pid in members - before.get(idx, set()):
                prog = programs.get(pid)
                # A migrant is a copy; upstream records its origin in metadata.
                meta = getattr(prog, "metadata", {}) or {}
                src = meta.get("migrant_from_island")
                if src is None:
                    parent = getattr(prog, "parent_id", None)
                    for sidx, smembers in before.items():
                        if parent and parent in smembers:
                            src = sidx
                            break
                migrations.append({
                    "source_island": src, "target_island": idx,
                    "candidate_id": getattr(prog, "parent_id", None) or pid,
                    "new_candidate_id": pid,
                })
        emit(self._ev(
            EventType.ISLAND_MIGRATION_COMPLETED, Component.DATABASE,
            generation=generation, duration_ms=duration_ms,
            summary=f"migration completed ({len(migrations)} migrants)",
            metrics={"migrants": float(len(migrations))},
            metadata={"migrations": migrations},
        ))
        for idx in after:
            self._emit_island_update(db, idx, generation)

    # -- evaluator -----------------------------------------------------

    def _install_evaluator(self) -> None:
        try:
            from openevolve.evaluator import Evaluator
        except ImportError:
            return
        inst = self

        def factory(original: Callable) -> Callable:
            async def wrapper(ev_self, program_code, program_id="", *a, **kw):
                span = new_id("span_")
                eval_id = new_id("eval_")
                t0 = time.perf_counter()
                started_at = time.time()
                cfg = getattr(ev_self, "config", None)
                emit(inst._ev(
                    EventType.EVALUATOR_STARTED, Component.EVALUATOR,
                    span_id=span, candidate_id=program_id or None,
                    summary=f"evaluating {program_id or '(anonymous)'}",
                    metadata={
                        "evaluation_id": eval_id,
                        "evaluator_id": getattr(cfg, "evaluation_file", None),
                        "timeout": getattr(cfg, "timeout", None),
                        "cascade": bool(getattr(cfg, "cascade_evaluation", False)),
                        "started_at": started_at,
                        "code_hash": _hash(program_code),
                    },
                ))
                emit(inst._ev(
                    EventType.CANDIDATE_EVALUATION_STARTED, Component.EVALUATOR,
                    span_id=span, candidate_id=program_id or None,
                    summary="evaluation started",
                ))
                try:
                    result = await original(ev_self, program_code, program_id, *a, **kw)
                except Exception as exc:
                    dur = (time.perf_counter() - t0) * 1000.0
                    err = {"type": type(exc).__name__, "message": str(exc)}
                    for et in (EventType.EVALUATOR_FAILED,
                               EventType.CANDIDATE_EVALUATION_COMPLETED):
                        emit(inst._ev(
                            et, Component.EVALUATOR, span_id=span,
                            candidate_id=program_id or None, duration_ms=dur,
                            status=Status.FAILED, summary=f"evaluation failed: {exc}",
                            error=err,
                            metadata={"evaluation_id": eval_id, "started_at": started_at,
                                      "failure_class": type(exc).__name__},
                        ))
                    raise

                dur = (time.perf_counter() - t0) * 1000.0
                metrics = {k: float(v) for k, v in (result or {}).items()
                           if isinstance(v, (int, float, bool))}
                fitness = _fitness(result or {})
                if fitness is not None:
                    metrics.setdefault("combined_score", fitness)
                # Upstream signals evaluator failure through an `error` metric key
                # rather than by raising, so treat that as failed.
                failed = "error" in (result or {})
                emit(inst._ev(
                    EventType.EVALUATOR_COMPLETED if not failed else EventType.EVALUATOR_FAILED,
                    Component.EVALUATOR, span_id=span, candidate_id=program_id or None,
                    duration_ms=dur, status=Status.OK if not failed else Status.FAILED,
                    summary=f"evaluation {'completed' if not failed else 'failed'}"
                            f" ({len(metrics)} metrics)",
                    metrics=metrics,
                    metadata={"evaluation_id": eval_id, "started_at": started_at,
                              "raw_keys": list((result or {}).keys())},
                ))
                emit(inst._ev(
                    EventType.CANDIDATE_EVALUATION_COMPLETED, Component.EVALUATOR,
                    span_id=span, candidate_id=program_id or None, duration_ms=dur,
                    status=Status.OK if not failed else Status.FAILED,
                    summary="evaluation completed", metrics=metrics,
                ))
                return result

            return wrapper

        self._patch(Evaluator, "evaluate_program", factory)

    # -- LLM -----------------------------------------------------------

    def _install_llm(self) -> None:
        try:
            from openevolve.llm.openai import OpenAILLM
        except ImportError:
            return
        inst = self

        def factory(original: Callable) -> Callable:
            async def wrapper(llm_self, system_message, messages, *a, **kw):
                span = new_id("span_")
                req_id = new_id("mreq_")
                t0 = time.perf_counter()
                started_at = time.time()
                model = getattr(llm_self, "model", None)
                api_base = getattr(llm_self, "api_base", None)
                provider = _provider_of(api_base)
                prompt_text = "\n\n".join(
                    m.get("content", "") for m in ([{"content": system_message}] + list(messages or []))
                )
                base_meta = {
                    "request_id": req_id, "model": model, "api_base": api_base,
                    "provider": provider, "started_at": started_at,
                    "role": _generation_role(),
                    "params": {
                        "temperature": getattr(llm_self, "temperature", None),
                        "top_p": getattr(llm_self, "top_p", None),
                        "max_tokens": getattr(llm_self, "max_tokens", None),
                    },
                }
                emit(inst._ev(
                    EventType.MODEL_REQUEST_STARTED, Component.LLM, span_id=span,
                    summary=f"model request → {model}",
                    input={"prompt": prompt_text}, metadata=base_meta,
                ))
                try:
                    response = await original(llm_self, system_message, messages, *a, **kw)
                except Exception as exc:
                    dur = (time.perf_counter() - t0) * 1000.0
                    text = str(exc).lower()
                    limited = "429" in text or "rate limit" in text or "too many requests" in text
                    emit(inst._ev(
                        EventType.MODEL_RATE_LIMITED if limited else EventType.MODEL_REQUEST_FAILED,
                        Component.LLM, span_id=span, duration_ms=dur, status=Status.FAILED,
                        summary=f"model request failed: {exc}",
                        error={"type": type(exc).__name__, "message": str(exc)},
                        metadata={**base_meta, "rate_limited": int(limited)},
                    ))
                    raise

                dur = (time.perf_counter() - t0) * 1000.0
                usage = _extract_usage(llm_self, response)
                completed_meta = dict(base_meta)
                if getattr(llm_self, "last_stop_reason", None):
                    completed_meta["stop_reason"] = llm_self.last_stop_reason
                if getattr(llm_self, "last_response_model", None):
                    # What the provider says it served can differ from what we
                    # asked for (aliases, silent substitutions); record both.
                    completed_meta["served_model"] = llm_self.last_response_model
                # A request through the OE-MAX broker was asked for by alias.
                # The provider and model recorded here are the ones that did
                # the work — otherwise every route through the broker is
                # indistinguishable, and the alias survives as requested_model.
                route = getattr(llm_self, "last_route", None)
                if route:
                    completed_meta["requested_model"] = model
                    completed_meta["requested_provider"] = provider
                    provider = route.get("provider") or provider
                    model = route.get("model") or model
                    completed_meta["provider"] = provider
                    completed_meta["model"] = model
                    completed_meta["route_attempt"] = route.get("attempt")
                    completed_meta["finish_reason"] = route.get("finish_reason")
                    if route.get("reasoning_tokens") is not None:
                        completed_meta["reasoning_tokens"] = route["reasoning_tokens"]
                llm_self.last_route = None
                # Publish this request to the task's context so the candidate
                # it produces can name it. Set before emitting so an emitter
                # that inspects context sees a consistent view.
                try:
                    _publish_generation({
                        "request_id": req_id, "provider": provider, "model": model,
                        "latency_ms": dur,
                        "tokens": int((usage or {}).get("total_tokens") or 0),
                        "reasoning_tokens": int(
                            completed_meta.get("reasoning_tokens")
                            or ((getattr(llm_self, "last_usage", None) or {})
                                .get("reasoning_tokens") or 0)
                        ),
                        "at": time.time(),
                    })
                except Exception:
                    pass
                emit(inst._ev(
                    EventType.MODEL_REQUEST_COMPLETED, Component.LLM, span_id=span,
                    duration_ms=dur, summary=f"model request completed ({model})",
                    input={"prompt": prompt_text},
                    output={"response": response if isinstance(response, str) else str(response)},
                    metrics=usage, metadata=completed_meta,
                ))
                # Clear so a later request cannot inherit stale usage if its own
                # response carries none.
                llm_self.last_usage = None
                return response

            return wrapper

        self._patch(OpenAILLM, "generate_with_context", factory)

        # Upstream's _call_api returns only the message content and discards the
        # provider's `usage` block, so token counts are unavailable to the
        # wrapper above. Capturing the raw response here — additively, without
        # editing upstream — is what makes the Models table show real token
        # accounting instead of a blank column.
        def call_api_factory(original: Callable) -> Callable:
            async def wrapper(llm_self, params, *a, **kw):
                response_holder: Dict[str, Any] = {}
                client = getattr(llm_self, "client", None)
                if client is None:
                    return await original(llm_self, params, *a, **kw)

                completions = client.chat.completions
                real_create = completions.create

                def capturing_create(**kwargs):
                    resp = real_create(**kwargs)
                    response_holder["response"] = resp
                    return resp

                try:
                    completions.create = capturing_create  # type: ignore[method-assign]
                    result = await original(llm_self, params, *a, **kw)
                finally:
                    completions.create = real_create  # type: ignore[method-assign]

                resp = response_holder.get("response")
                if resp is not None:
                    usage = getattr(resp, "usage", None)
                    if usage is not None:
                        llm_self.last_usage = {
                            "prompt_tokens": getattr(usage, "prompt_tokens", None),
                            "completion_tokens": getattr(usage, "completion_tokens", None),
                            "total_tokens": getattr(usage, "total_tokens", None),
                        }
                    choices = getattr(resp, "choices", None) or []
                    if choices:
                        llm_self.last_stop_reason = getattr(choices[0], "finish_reason", None)
                    llm_self.last_response_model = getattr(resp, "model", None)
                    llm_self.last_route = _broker_route(resp)
                return result

            return wrapper

        self._patch(OpenAILLM, "_call_api", call_api_factory)

    # -- controller ----------------------------------------------------

    def _install_controller(self) -> None:
        try:
            from openevolve.controller import OpenEvolve
        except ImportError:
            return
        inst = self

        def run_factory(original: Callable) -> Callable:
            async def wrapper(ctl_self, *a, **kw):
                t0 = time.perf_counter()
                emit(inst._ev(
                    EventType.EXPERIMENT_STARTED, Component.CONTROLLER,
                    summary="evolution run started",
                    metadata={"provenance": _provenance(ctl_self),
                              "output_dir": getattr(ctl_self, "output_dir", None)},
                ))
                try:
                    result = await original(ctl_self, *a, **kw)
                except BaseException as exc:
                    # BaseException so a KeyboardInterrupt still records an outcome.
                    emit(inst._ev(
                        EventType.EXPERIMENT_FAILED if not isinstance(exc, KeyboardInterrupt)
                        else EventType.EXPERIMENT_STOPPED,
                        Component.CONTROLLER,
                        duration_ms=(time.perf_counter() - t0) * 1000.0,
                        status=Status.FAILED if not isinstance(exc, KeyboardInterrupt)
                        else Status.CANCELLED,
                        summary=f"run ended: {type(exc).__name__}: {exc}",
                        error={"type": type(exc).__name__, "message": str(exc)},
                    ))
                    raise
                db = getattr(ctl_self, "database", None)
                metrics = {}
                if db is not None:
                    metrics["candidates"] = float(len(getattr(db, "programs", {}) or {}))
                if result is not None:
                    f = _fitness(getattr(result, "metrics", {}) or {})
                    if f is not None:
                        metrics["best_fitness"] = f
                emit(inst._ev(
                    EventType.EXPERIMENT_COMPLETED, Component.CONTROLLER,
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                    candidate_id=getattr(result, "id", None),
                    summary="evolution run completed", metrics=metrics,
                ))
                return result

            return wrapper

        self._patch(OpenEvolve, "run", run_factory)

        def ckpt_factory(original: Callable) -> Callable:
            def wrapper(ctl_self, iteration, *a, **kw):
                t0 = time.perf_counter()
                try:
                    result = original(ctl_self, iteration, *a, **kw)
                except Exception as exc:
                    emit(inst._ev(
                        EventType.CHECKPOINT_FAILED, Component.CONTROLLER,
                        iteration=iteration, status=Status.FAILED,
                        summary=f"checkpoint failed: {exc}",
                        error={"type": type(exc).__name__, "message": str(exc)},
                    ))
                    raise
                path = os.path.join(
                    getattr(ctl_self, "output_dir", "") or "", "checkpoints",
                    f"checkpoint_{iteration}",
                )
                db = getattr(ctl_self, "database", None)
                size = _dir_size(path)
                emit(inst._ev(
                    EventType.CHECKPOINT_CREATED, Component.CONTROLLER,
                    iteration=iteration,
                    duration_ms=(time.perf_counter() - t0) * 1000.0,
                    summary=f"checkpoint {iteration} created",
                    metrics={
                        "size_bytes": float(size),
                        "num_programs": float(len(getattr(db, "programs", {}) or {}) if db else 0),
                    },
                    metadata={"path": path, "checkpoint_id": f"{inst.run_id}:{iteration}"},
                ))
                return result

            return wrapper

        self._patch(OpenEvolve, "_save_checkpoint", ckpt_factory)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _provider_of(api_base: Optional[str]) -> str:
    if not api_base:
        return "openai"
    b = api_base.lower()
    if "opencode" in b or "zen" in b:
        return "opencode_zen"
    if "nvidia" in b or "nim" in b or "integrate.api" in b:
        return "nvidia_nim"
    if "openrouter" in b:
        return "openrouter"
    if "localhost" in b or "127.0.0.1" in b:
        return "local"
    if "anthropic" in b:
        return "anthropic"
    if "openai" in b:
        return "openai"
    return "custom"


def _extract_usage(llm_self: Any, response: Any) -> Dict[str, float]:
    """
    Read token usage from whatever the client last recorded.

    Upstream's generate_with_context returns only the text, so usage has to come
    from a side channel. We look for common attribute names and return an empty
    dict when none exist — an absent metric is reported as absent, never as 0,
    so the UI cannot present a fabricated token count.
    """
    usage: Dict[str, float] = {}
    for attr in ("last_usage", "_last_usage", "usage"):
        u = getattr(llm_self, attr, None)
        if not u:
            continue
        get = u.get if isinstance(u, dict) else lambda k, d=None: getattr(u, k, d)
        for src, dst in (
            ("prompt_tokens", "prompt_tokens"),
            ("completion_tokens", "completion_tokens"),
            ("total_tokens", "total_tokens"),
        ):
            v = get(src)
            if isinstance(v, (int, float)):
                usage[dst] = float(v)
        if usage:
            break
    return usage


def _dir_size(path: str) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total


def _provenance(controller: Any) -> Dict[str, Any]:
    """Everything needed to reconstruct this run (SOURCE_OF_TRUTH section 17)."""
    import platform
    import sys

    prov: Dict[str, Any] = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "pid": os.getpid(),
        "captured_at": time.time(),
    }
    try:
        import openevolve

        prov["openevolve_version"] = getattr(openevolve, "__version__", None)
    except Exception:
        pass
    try:
        import json as _json
        from pathlib import Path

        here = Path(__file__).resolve().parents[2] / "UPSTREAM.json"
        if here.exists():
            prov["upstream"] = _json.loads(here.read_text())
    except Exception:
        pass
    cfg = getattr(controller, "config", None)
    if cfg is not None:
        prov["random_seed"] = getattr(cfg, "random_seed", None)
        prov["max_iterations"] = getattr(cfg, "max_iterations", None)
        llm = getattr(cfg, "llm", None)
        if llm is not None:
            models = getattr(llm, "models", None) or []
            prov["models"] = [
                {"name": getattr(m, "name", None), "api_base": getattr(m, "api_base", None),
                 "weight": getattr(m, "weight", None)}
                for m in models
            ]
    prov["output_dir"] = getattr(controller, "output_dir", None)
    return prov


# --------------------------------------------------------------------------
# auto-install (used by worker processes and by `evolution run`)
# --------------------------------------------------------------------------

_active: Optional[EngineInstrumentation] = None
_active_pid: Optional[int] = None


def auto_install_from_env() -> Optional[EngineInstrumentation]:
    """
    Configure the bus and install hooks from environment variables.

    Worker processes inherit these, so evaluation and model calls made inside a
    ProcessPoolExecutor worker are instrumented exactly like in-process ones.
    Absent EVOLUTION_TELEMETRY, this is a no-op — which is what keeps the plain
    upstream CLI byte-for-byte unaffected.

    Keyed by PID for the same reason configure_bus is: a forked worker inherits
    `_active` from its parent and would otherwise return early, leaving the
    child with hooks that emit into a bus with no live worker thread. Rebuilding
    per process is what makes worker-side model calls and evaluations visible.
    """
    global _active, _active_pid
    pid = os.getpid()
    if _active is not None and _active_pid == pid:
        return _active
    if _active is not None:
        # Inherited across fork: the hooks are still installed in this child's
        # copy of the classes, so re-installing would double-wrap. Drop the
        # handle and rebuild the bus below.
        _active = None
    if os.environ.get(ENV_ENABLED, "").lower() not in ("1", "true", "yes", "on"):
        return None
    run_id = os.environ.get(ENV_RUN_ID)
    if not run_id:
        return None
    configure_bus(
        ndjson_path=os.environ.get(ENV_EVENT_LOG),
        socket_port=int(os.environ[ENV_COLLECTOR_PORT])
        if os.environ.get(ENV_COLLECTOR_PORT) else None,
    )
    from .redaction import default_redactor

    default_redactor().register_env(
        "OPENAI_API_KEY", "NVIDIA_API_KEY", "NIM_API_KEY",
        "OPENCODE_API_KEY", "ZEN_API_KEY", "EVOLUTION_PROVIDER_KEY",
        "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY",
    )
    _active = EngineInstrumentation(
        run_id=run_id,
        experiment_id=os.environ.get(ENV_EXPERIMENT_ID),
    ).install()
    _active_pid = pid
    # Idempotent: under fork the child already inherited the parent's wrapper
    # and the guard flag makes this a no-op. It matters when it did not.
    try:
        install_worker_attribution_hook()
    except Exception as exc:  # pragma: no cover
        logger.debug("attribution hook install failed: %r", exc)
    return _active


def install_worker_hook() -> None:
    """
    Make ProcessPoolExecutor workers self-instrument.

    process_parallel._worker_init runs first in each worker; wrapping it is the
    one reliable place to install telemetry inside a process we do not spawn
    ourselves.
    """
    try:
        from openevolve import process_parallel
    except ImportError:
        return
    original = getattr(process_parallel, "_worker_init", None)
    if original is None or getattr(original, "__evolution_instrumented__", False):
        return

    @functools.wraps(original)
    def wrapper(*a, **kw):
        result = original(*a, **kw)
        try:
            auto_install_from_env()
        except Exception as exc:
            logger.debug("worker telemetry install failed: %r", exc)
        return result

    wrapper.__evolution_instrumented__ = True  # type: ignore[attr-defined]
    process_parallel._worker_init = wrapper
    install_worker_attribution_hook()


def _emit_iteration_completed(result: Any, iteration: Any, duration_ms: float) -> None:
    """
    Mark the end of one evolution iteration, from the worker that ran it.

    Upstream emits no generation boundary, so `runs.iterations_done` sat at 0
    for the whole of every run — and a progress bar reading "0 / 12" while the
    run is plainly working is exactly the plausible-looking wrong number the
    no-fake-data rule exists to prevent.

    This frame is the right place because it is reached once per iteration
    whether or not the iteration produced anything: an iteration that returned
    "No valid diffs found" still happened, still consumed a request, and still
    has to count as progress. Attributing progress only to successful
    iterations would make a degraded route look like a stalled run.
    """
    inst = _active
    if inst is None or iteration is None:
        return
    try:
        error = getattr(result, "error", None)
        emit(inst._ev(
            EventType.GENERATION_COMPLETED, Component.CONTROLLER,
            iteration=int(iteration), duration_ms=duration_ms,
            status=Status.FAILED if error else Status.OK,
            summary=(f"iteration {iteration} produced nothing: {error}" if error
                     else f"iteration {iteration} completed"),
            metadata={"produced_candidate": bool(
                getattr(result, "child_program_dict", None)),
                "error": error},
        ))
    except Exception as exc:  # pragma: no cover - telemetry must never break a run
        logger.debug("iteration-completed emit failed: %r", exc)


def install_worker_attribution_hook() -> None:
    """
    Carry generation provenance across the worker→main process boundary.

    `_run_iteration_worker` is the only frame that spans both halves of the
    problem: it runs *in the worker*, where the model request is made, and it
    returns the `SerializableResult` the main process turns back into a
    `Program` and hands to `database.add`. Stamping the attribution onto
    `child_program_dict["metadata"]` here is therefore the whole fix — the
    dict is pickled to the main process, and `Program(**dict)` restores it.

    Wrapping is safe for the process pool: `functools.wraps` preserves
    `__module__`/`__qualname__`, and the module attribute is rebound to this
    wrapper, so pickle-by-reference resolves to the same object it pickled.
    """
    try:
        from openevolve import process_parallel
    except ImportError:
        return
    original = getattr(process_parallel, "_run_iteration_worker", None)
    if original is None or getattr(original, "__evolution_instrumented__", False):
        return

    @functools.wraps(original)
    def wrapper(iteration=None, *a, **kw):
        _begin_worker_attribution()
        t0 = time.perf_counter()
        result = original(iteration, *a, **kw)
        try:
            rec = _take_worker_attribution()
            child = getattr(result, "child_program_dict", None)
            if rec and isinstance(child, dict):
                md = child.get("metadata")
                if not isinstance(md, dict):
                    md = {}
                    child["metadata"] = md
                md[ATTRIBUTION_KEY] = dict(rec)
        except Exception as exc:  # attribution is observability, never control
            logger.debug("worker attribution failed: %r", exc)
        _emit_iteration_completed(result, iteration, (time.perf_counter() - t0) * 1000.0)
        return result

    wrapper.__evolution_instrumented__ = True  # type: ignore[attr-defined]
    process_parallel._run_iteration_worker = wrapper
