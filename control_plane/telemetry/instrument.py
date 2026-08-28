"""
Engine instrumentation.

Design choice that shapes this whole file: we instrument by *wrapping public
methods at runtime* and observing real engine state before/after the call —
not by editing OpenEvolve's source.

Why: the fork must keep merging upstream (SOURCE_OF_TRUTH section 27). Editing
call sites across database.py / controller.py / evaluator.py would create a
patch surface that conflicts on every upstream release. Wrapping keeps
docs/patch-surface.md empty for telemetry, so an upstream merge is a fast-forward.

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
    global _worker_generation, _worker_generation_pid, _worker_operator
    global _worker_island, _worker_island_count
    _worker_generation = None
    _worker_operator = None
    _worker_island = None
    _worker_island_count = 0
    _worker_generation_pid = os.getpid()
    try:
        from . import multi_offspring

        multi_offspring.reset()
    except Exception:  # pragma: no cover - import cannot realistically fail
        pass


def _publish_generation(record: Dict[str, Any]) -> None:
    """
    Record a completed model request for the candidate it will produce.

    Both channels are written: the ContextVar for the in-process path (where
    generation and `add` share a task context) and the process-local slot for
    the worker path (where they do not share a process at all).
    """
    global _worker_generation
    if _worker_operator and not record.get("operator"):
        record = {**record, "operator": _worker_operator}
    if island_policies_enabled() and _worker_island_count > 0 \
            and not record.get("island_policy"):
        from oe_max.search.policies import policy_for

        record = {**record,
                  "island_policy": policy_for(_worker_island,
                                              _worker_island_count).name}
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


# Whether to steer each mutation with a named operator from the OE-MAX
# taxonomy. Off unless asked for, because it changes what the model is asked
# and would otherwise confound the stock-vs-MAX comparison: a difference in
# results has to be attributable to one change at a time.
ENV_OPERATORS = "OE_MAX_OPERATORS"

# The operator chosen for the iteration this worker is running, so the request
# it makes can be labelled with it.
_worker_operator: Optional[str] = None

# Which island this iteration's parent came from, and how many islands exist.
# Set by the worker wrapper *before* the original runs, because the prompt is
# built inside it and upstream's `build_prompt` is never told the island.
_worker_island: Optional[int] = None
_worker_island_count: int = 0

# Give each island a different search posture rather than the same one. Separate
# from operator steering because it is a further claim: steering says "name the
# mutation", policies say "this island is for a different kind of mutation".
ENV_ISLAND_POLICIES = "OE_MAX_ISLAND_POLICIES"

# Let the bandit pick the operator instead of uniform random. Separate from
# operator steering because it is a further claim: steering says "name the
# mutation class", this says "and let measured reward choose which".
#
# Off by default, and it must stay that way until somebody measures whether it
# beats uniform. It also makes a run non-reproducible in a way seeding cannot
# fix — the choice depends on rewards from earlier iterations, so a rerun with
# the same seed diverges the moment a score differs.
ENV_OPERATOR_BANDIT = "OE_MAX_OPERATOR_BANDIT"


def island_policies_enabled() -> bool:
    return os.environ.get(ENV_ISLAND_POLICIES, "").lower() in ("1", "true", "yes", "on")


def operators_enabled() -> bool:
    return os.environ.get(ENV_OPERATORS, "").lower() in ("1", "true", "yes", "on")


def operator_bandit_enabled() -> bool:
    """
    Bandit selection requires operator steering: without a named operator there
    is nothing to attribute reward to, so enabling this alone would be a flag
    that silently does nothing.
    """
    if not operators_enabled():
        return False
    return os.environ.get(ENV_OPERATOR_BANDIT, "").lower() in ("1", "true", "yes", "on")


def _bandit_store(run_id: Optional[str]):
    """
    The per-run bandit state file, or None.

    Per *run*, deliberately. A bandit that carried evidence between runs would
    learn across different tasks, different seeds and different providers, and
    the operator that suits one problem is not the operator that suits the
    next. It would also make the first iteration of every run depend on
    whichever run happened to precede it, which is untraceable.
    """
    if not run_id:
        return None
    try:
        from oe_max.search.bandit_store import BanditStore
        from oe_max.search.operators import OPERATORS

        root = os.environ.get("EVOLUTION_WORKSPACE") or os.path.join(".evolution", "workspace")
        path = os.path.join(root, "bandits", f"{run_id}.json")
        return BanditStore(path, [op.value for op in OPERATORS])
    except Exception as exc:
        logger.debug("bandit store unavailable: %r", exc)
        return None


def _select_operator(run_id: Optional[str], iteration: Any,
                     *, has_failure: bool, has_second_parent: bool) -> Optional[str]:
    """
    Pick the mutation class to ask for.

    Uniform random, deliberately, and not yet the bandit. The bandit learns
    from per-operator reward, and there is no per-operator reward until
    mutations are labelled — which is what this function starts. Wiring the
    bandit first would have it learn from a prior nobody measured. Measure,
    then optimise.

    Seeded from (run_id, iteration) so a rerun of the same run picks the same
    operators. Workers are separate processes with no shared state, so a
    derived seed is also the only way to keep the choice reproducible without
    inventing a coordination channel.

    Context filtering is upstream's own: asking for COUNTEREXAMPLE_REPAIR with
    no counterexample produces a vague request, and the resulting bad numbers
    would be blamed on a perfectly good operator.
    """
    try:
        import random

        from oe_max.search.operators import applicable

        choices = applicable(has_failure=has_failure,
                             has_second_parent=has_second_parent)
        if not choices:
            return None
        rng = random.Random(f"{run_id}:{iteration}")
        if island_policies_enabled() and _worker_island_count > 0:
            from oe_max.search.policies import choose, policy_for

            policy = policy_for(_worker_island, _worker_island_count)
            picked = choose(policy, choices, rng)
            return picked.value if picked else None

        if operator_bandit_enabled():
            # Reward is observed in the main process and selection happens
            # here, in a worker, so the bandit's two halves never share memory.
            # The state file is the channel — see oe_max/search/bandit_store.
            store = _bandit_store(run_id)
            if store is not None:
                picked = store.select([c.value for c in choices])
                if picked is not None:
                    return str(picked)
            # Falling through to uniform is the point: an unreadable state file
            # must cost a little exploitation, never the mutation.

        return rng.choice(choices).value
    except Exception as exc:  # steering is an enhancement, never a requirement
        logger.debug("operator selection failed: %r", exc)
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


def _parent_fitness(db: Any, program: Any) -> Optional[float]:
    """The parent's score, for the delta the reward is built from."""
    parent_id = getattr(program, "parent_id", None)
    if not parent_id:
        return None
    parent = (getattr(db, "programs", {}) or {}).get(parent_id)
    if parent is None:
        return None
    cfg = getattr(db, "config", None)
    dims = list(getattr(cfg, "feature_dimensions", []) or [])
    return _fitness(dict(getattr(parent, "metrics", {}) or {}), dims)


def _reward_operator(db: Any, program: Any, *, accepted: bool,
                     fitness: Optional[float]) -> None:
    """
    Credit the operator that produced this candidate.

    This half runs in the MAIN process, where the score exists. Selection runs
    in a worker, where it does not. The two never share memory — see
    docs/gotchas.md — so the bandit's evidence goes through a file.

    Migrants are excluded. `_migrate_programs` copies metadata wholesale into
    the copy, so a migrated program carries the operator of the mutation that
    made the *original*, and rewarding it again would count one mutation
    several times — once per island it reaches. That is the same trap that made
    two analysis modules measure the wrong population.
    """
    if not operator_bandit_enabled():
        return
    try:
        md = getattr(program, "metadata", None) or {}
        if md.get("migrant"):
            return
        operator = (_attribution_of(program) or {}).get("operator")
        if not operator:
            return
        store = _bandit_store(_active.run_id if _active else None)
        if store is None:
            return

        from oe_max.search.bandit import reward_from_outcome

        delta: Optional[float] = None
        if accepted and fitness is not None:
            parent = _parent_fitness(db, program)
            if parent is not None:
                delta = fitness - parent
        store.update(operator, reward_from_outcome(accepted=accepted,
                                                   fitness_delta=delta))
    except Exception as exc:   # learning must never cost a candidate
        logger.debug("operator reward failed: %r", exc)


def _provenance_flags(program: Any) -> Dict[str, Any]:
    """
    How this candidate came to exist, as flags the projections can filter on.

    `migrant` is upstream's own marker for a copy made by `_migrate_programs`;
    the rest are OE-MAX features. They are emitted whether true or false so a
    consumer can tell "not a migrant" from "this run predates the flag".
    """
    md = getattr(program, "metadata", None) or {}
    flags = {
        "migrant": bool(md.get("migrant")),
        "multi_offspring": bool(md.get("multi_offspring")),
        "seed_forge": bool(md.get("seed_forge")),
    }
    for key in ("forge_origin", "forge_detail", "island_policy"):
        if md.get(key):
            flags[key] = md[key]
    return flags


def _attribution_fields(rec: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The provenance block attached to every candidate event."""
    rec = rec or {}
    return {
        "generating_request_id": rec.get("request_id"),
        "generating_provider": rec.get("provider"),
        "generating_model": rec.get("model"),
        "generating_latency_ms": rec.get("latency_ms"),
        "generating_tokens": rec.get("tokens"),
        "generating_operator": rec.get("operator"),
        # Which island policy asked for this mutation. Without it the policy
        # layer is invisible in the stored data and cannot be evaluated.
        "generating_island_policy": rec.get("island_policy"),
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


def _maybe_seed(inst: Any, db: Any, program: Any) -> None:
    """
    Turn the first program into a starting population, if asked.

    Runs after the seed's own `add` so upstream's bookkeeping — island
    assignment, best-program tracking, the feature map — is already settled and
    the variants land in a consistent database rather than a half-built one.
    """
    from . import seed_hook

    hook = seed_hook.get_hook()
    if hook is None:
        return
    try:
        added = hook.maybe_seed(db, program)
        if not added:
            return
        emit(inst._ev(
            EventType.POPULATION_UPDATED, Component.DATABASE, iteration=0,
            summary=f"seed forge added {len(added)} variants",
            metrics={"variants": float(len(added))},
            metadata={"seed_forge": hook.report},
        ))
    except Exception as exc:   # a forge failure must not cost the run its seed
        logger.debug("seed forge hook failed: %r", exc)


def _maybe_verify(inst: Any, db: Any, program: Any, before_best: Any,
                  iteration: Any) -> None:
    """
    Run V1 verification on the candidates worth the cost.

    Two triggers, both chosen because getting them wrong is expensive:
    a new champion, because the run now optimises around it and if it is wrong
    everything downstream is wrong; and a jump far beyond this run's own
    history of improvements, which is the exact shape of a candidate that
    stopped solving the problem and started reporting a number.

    A failure does **not** remove the candidate. This is instrumentation, and
    instrumentation that silently deletes the engine's work would make the fork
    behave differently from upstream in a way no test would catch. The event
    carries the counterexample; enforcement is a separate and explicit choice.
    """
    from . import verification_hook

    live = verification_hook.get_verifier()
    if live is None:
        return
    try:
        after_best = getattr(db, "best_program_id", None)
        is_new_best = bool(after_best == program.id and after_best != before_best)

        cfg = getattr(db, "config", None)
        dims = list(getattr(cfg, "feature_dimensions", []) or [])
        score = _fitness(dict(getattr(program, "metrics", {}) or {}), dims)
        parent_metrics = (getattr(program, "metadata", None) or {}).get("parent_metrics")
        parent_score = (_fitness(dict(parent_metrics), dims)
                        if isinstance(parent_metrics, dict) else None)
        delta = (score - parent_score
                 if score is not None and parent_score is not None else None)

        decision = live.should_verify(is_new_best=is_new_best, delta=delta)
        if decision["trigger"] == "suspicious_jump":
            emit(inst._ev(
                EventType.CANDIDATE_SUSPICIOUS, Component.VERIFIER,
                candidate_id=program.id, iteration=iteration, status=Status.WARNING,
                summary=decision["reason"], metadata=decision["suspicion"],
            ))
        if not decision["verify"]:
            return

        code = getattr(program, "code", None)
        if not code:
            return

        emit(inst._ev(
            EventType.CANDIDATE_VERIFICATION_STARTED, Component.VERIFIER,
            candidate_id=program.id, iteration=iteration,
            summary=f"verifying {program.id}: {decision['trigger']}",
            metadata={"trigger": decision["trigger"], "reason": decision["reason"]},
        ))
        report = live.verify(code, program.id, score)
        if report is None:
            return          # already verified; a champion re-confirmed is not re-run

        passed = report.passed
        emit(inst._ev(
            EventType.CANDIDATE_VERIFICATION_PASSED if passed
            else EventType.CANDIDATE_VERIFICATION_FAILED,
            Component.VERIFIER, candidate_id=program.id, iteration=iteration,
            duration_ms=report.duration_ms,
            status=Status.OK if passed else Status.FAILED,
            summary=report.summary(),
            metadata={"trigger": decision["trigger"], **report.to_dict()},
        ))
    except Exception as exc:   # verification is observability, never control
        logger.debug("verification hook failed: %r", exc)


def _add_siblings(db, program: Any, iteration: Any, target_island: Any) -> None:
    """
    Put the extra offspring into the population, next to the primary child.

    Deliberately a normal `add`: the siblings go through the same MAP-Elites
    placement, the same novelty gate and the same telemetry as any other
    candidate. Special-casing them would make their measured yield
    incomparable to everything else, which is the one thing a throughput
    experiment cannot afford.

    Re-entrancy is handled by stripping the siblings key from what is added —
    without that, a sibling carrying its own sibling list would recurse.
    """
    from . import multi_offspring

    md = getattr(program, "metadata", None)
    if not isinstance(md, dict):
        return
    siblings = md.pop(multi_offspring.SIBLINGS_KEY, None)
    if not siblings:
        return
    try:
        from openevolve.database import Program
    except ImportError:
        return

    for spec in siblings:
        try:
            spec = dict(spec)
            spec_md = dict(spec.get("metadata") or {})
            spec_md.pop(multi_offspring.SIBLINGS_KEY, None)
            spec["metadata"] = spec_md
            db.add(Program(**spec), iteration=iteration, target_island=target_island)
        except Exception as exc:
            # One bad sibling must not cost the iteration that produced it.
            logger.debug("sibling add failed: %r", exc)


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
                _add_siblings(db_self, program, iteration, target_island)
                _maybe_seed(inst, db_self, program)
                _maybe_verify(inst, db_self, program, before_best, iteration)
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
            # A rejected candidate is a real outcome for its operator, not a
            # missing one. Recording only accepted candidates would teach the
            # bandit that an operator producing nothing but duplicates is
            # indistinguishable from one that is never tried.
            _reward_operator(db, program, accepted=False, fitness=None)
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
                # Provenance flags, copied from the program rather than
                # inferred. Every one of these is read back by an analysis
                # module, and a flag that lives only on the in-memory Program
                # is a filter that silently never matches — `throughput` was
                # excluding migrants by a key the projection never wrote, and
                # `outcome` was including them for the same reason. Found by
                # running every feature at once and looking at the rows.
                **_provenance_flags(program),
                # Generation provenance — the link upstream does not provide.
                **_attribution_fields(gen_req),
            },
        ))

        _reward_operator(db, program, accepted=True, fitness=fitness)

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
        install_operator_hook()
        from . import sandbox_eval

        sandbox_eval.install(_active)
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
    # Rebinding above only reaches a forked child. A spawned one re-imports
    # process_parallel and gets the original back, so the initializer itself
    # has to come from a module the child will resolve to our code.
    install_pool_initializer_hook()
    install_worker_attribution_hook()
    install_operator_hook()
    try:
        from . import sandbox_eval

        sandbox_eval.install(None)
    except Exception as exc:  # pragma: no cover
        logger.debug("sandbox eval install failed: %r", exc)


def _worker_bootstrap(original_initializer, *initargs):
    """
    Pool-worker entry point that survives `spawn`.

    This is a module-level function on purpose: under `spawn` the initializer is
    pickled **by reference** (module path + qualified name) and re-resolved by
    importing that module in the child. `install_worker_hook` rebinds
    `openevolve.process_parallel._worker_init` to a wrapper, but that rebinding
    lives only in the parent's memory -- the child imports `process_parallel`
    fresh and gets upstream's ORIGINAL function back, so the wrapper (and with
    it every worker-side emit) silently disappears.

    Under `fork` the child inherits the parent's patched module, which is why
    this was invisible on Linux and why the measurements taken there were real.
    On Windows and macOS, where the default context is `spawn`, a run produced
    candidates but reported `model_requests: 0` and `iterations_done: 0` -- a
    plausible-looking zero, which is the specific thing the no-fake-data rule
    exists to prevent.

    Because this function lives in *our* package, the child resolves it to the
    real thing, and installing telemetry here happens before upstream's own
    initializer runs.
    """
    try:
        auto_install_from_env()
    except Exception as exc:  # pragma: no cover - telemetry never breaks a run
        logger.debug("worker telemetry bootstrap failed: %r", exc)
    if original_initializer is not None:
        original_initializer(*initargs)


def install_pool_initializer_hook() -> None:
    """
    Make every ProcessPoolExecutor worker self-instrument under `spawn`.

    Wraps the stdlib constructor rather than upstream's call site, because the
    call site is in `openevolve/` and that tree stays byte-identical. Patching
    the class reaches every construction of it regardless of how the name was
    imported, since there is only one class object.

    The substitution is confined to runs that asked for telemetry: with
    EVOLUTION_TELEMETRY unset the initializer is left exactly as the caller
    passed it, which is what keeps the plain upstream CLI unaffected.
    """
    import concurrent.futures.process as _cfp

    original = _cfp.ProcessPoolExecutor.__init__
    if getattr(original, "__evolution_instrumented__", False):
        return

    @functools.wraps(original)
    def wrapper(self, max_workers=None, mp_context=None,
                initializer=None, initargs=(), **kw):
        if os.environ.get(ENV_ENABLED, "").lower() in ("1", "true", "yes", "on"):
            initializer = functools.partial(_worker_bootstrap, initializer)
        return original(self, max_workers=max_workers, mp_context=mp_context,
                        initializer=initializer, initargs=initargs, **kw)

    wrapper.__evolution_instrumented__ = True  # type: ignore[attr-defined]
    _cfp.ProcessPoolExecutor.__init__ = wrapper  # type: ignore[assignment]


def _record_worker_island(args: Any, kwargs: Any) -> None:
    """
    Note which island this iteration is working in.

    The island is in the snapshot the worker is handed, but never reaches
    `build_prompt` — so without capturing it here an island policy could not
    influence the one thing it exists to influence.
    """
    global _worker_island, _worker_island_count
    try:
        db_snapshot = kwargs.get("db_snapshot") if kwargs else None
        parent_id = kwargs.get("parent_id") if kwargs else None
        if db_snapshot is None and len(args) >= 1:
            db_snapshot = args[0]
        if parent_id is None and len(args) >= 2:
            parent_id = args[1]
        snapshot = db_snapshot or {}
        islands = snapshot.get("islands") or []
        _worker_island_count = len(islands)

        parent = (snapshot.get("programs") or {}).get(parent_id) or {}
        island = (parent.get("metadata") or {}).get("island")
        _worker_island = (int(island) if isinstance(island, int)
                          else snapshot.get("current_island"))
    except Exception as exc:  # pragma: no cover - policy is an enhancement
        logger.debug("island capture failed: %r", exc)
        _worker_island, _worker_island_count = None, 0


def _attach_siblings(result: Any, iteration: Any, args: Any, kwargs: Any) -> None:
    """
    Carry the extra offspring home from the worker.

    They ride on the primary child's metadata because that is the only thing
    that crosses the process boundary (§3.7). If the iteration produced no
    primary child there is nothing to ride on and nothing to carry: an
    alternative to a mutation that itself failed is not worth rescuing.
    """
    from . import multi_offspring

    if not multi_offspring.enabled():
        return
    child = getattr(result, "child_program_dict", None)
    if not isinstance(child, dict):
        multi_offspring.take_alternatives()      # discard; nothing to attach to
        return
    try:
        db_snapshot = kwargs.get("db_snapshot") if kwargs else None
        parent_id = kwargs.get("parent_id") if kwargs else None
        if db_snapshot is None and len(args) >= 1:
            db_snapshot = args[0]
        if parent_id is None and len(args) >= 2:
            parent_id = args[1]
        parent = ((db_snapshot or {}).get("programs") or {}).get(parent_id) or {}

        siblings = multi_offspring.build_siblings(
            parent_code=parent.get("code") or "",
            parent_id=parent_id or "",
            parent_metadata=dict(child.get("metadata") or {}),
            iteration=iteration,
            primary_code=child.get("code"),
            language=child.get("language") or "python",
        )
        if siblings:
            md = child.get("metadata")
            if not isinstance(md, dict):
                md = {}
                child["metadata"] = md
            md[multi_offspring.SIBLINGS_KEY] = siblings
    except Exception as exc:   # extra offspring are a bonus, never a requirement
        logger.debug("sibling construction failed: %r", exc)


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


def install_operator_hook() -> None:
    """
    Steer each mutation with a named operator class, and label it.

    Upstream issues one undifferentiated "improve this program" request, so
    every mutation is the same mutation and no credit assignment is possible:
    `route_quality.by_operator` had nowhere to get an operator from and was
    empty by construction. This is the missing half.

    The directive is appended to the **system** message, not the user message.
    The user message carries the program and the exact SEARCH/REPLACE format
    contract; appending to it risks the model treating the directive as part of
    the diff spec, and a broken diff format costs the whole request.

    Only the mutation sampler is steered. Upstream builds a second
    `PromptSampler` for evaluator feedback and marks it with
    `set_templates("evaluator_system_message")` — telling an evaluator to
    "substitute a fundamentally different algorithm" would corrupt the score
    rather than the candidate, which is much harder to notice.

    Off unless `OE_MAX_OPERATORS` is set. It changes what the model is asked,
    and turning it on by default would confound every comparison already
    recorded.
    """
    from . import multi_offspring

    if not operators_enabled() and not multi_offspring.enabled():
        return
    try:
        from openevolve.prompt.sampler import PromptSampler
    except ImportError:
        return
    original = getattr(PromptSampler, "build_prompt", None)
    if original is None or getattr(original, "__evolution_instrumented__", False):
        return

    @functools.wraps(original)
    def wrapper(sampler_self, *a, **kw):
        global _worker_operator
        prompt = original(sampler_self, *a, **kw)
        try:
            if getattr(sampler_self, "system_template_override", None):
                return prompt          # evaluator feedback, not a mutation
            if not isinstance(prompt, dict) or "system" not in prompt:
                return prompt
            # Both features decorate the same prompt; doing it in one place is
            # what keeps a prompt from being decorated twice.
            prompt = multi_offspring.install_prompt_hook(prompt)
            if not operators_enabled():
                return prompt
            artifacts = kw.get("program_artifacts") or {}
            op = _select_operator(
                _active.run_id if _active else None,
                kw.get("evolution_round"),
                has_failure=bool(artifacts),
                has_second_parent=bool(kw.get("inspirations")),
            )
            if not op:
                return prompt
            from oe_max.search.operators import OPERATORS, OperatorClass

            fragment = OPERATORS[OperatorClass(op)].prompt_fragment()
            prompt = dict(prompt)
            prompt["system"] = f"{prompt['system']}\n\n{fragment}"
            _worker_operator = op
        except Exception as exc:   # a steering failure must not lose the prompt
            logger.debug("operator steering failed: %r", exc)
        return prompt

    wrapper.__evolution_instrumented__ = True  # type: ignore[attr-defined]
    PromptSampler.build_prompt = wrapper  # type: ignore[method-assign]
    if operators_enabled():
        logger.info("OE-MAX operator steering enabled (%s)", ENV_OPERATORS)
    multi_offspring.install_parse_hooks()


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
        # Before the original, not after: it builds the prompt, and upstream's
        # build_prompt is never told which island the parent came from.
        _record_worker_island(a, kw)
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
        _attach_siblings(result, iteration, a, kw)
        _emit_iteration_completed(result, iteration, (time.perf_counter() - t0) * 1000.0)
        return result

    wrapper.__evolution_instrumented__ = True  # type: ignore[attr-defined]
    process_parallel._run_iteration_worker = wrapper
