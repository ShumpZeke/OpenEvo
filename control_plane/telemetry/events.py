"""
Evolution telemetry — typed universal event model.

Every value rendered in the Control Center must originate from an Event emitted
here. There is no second, "decorative" path: if the UI shows it, an event (or a
row derived from one) carries it.

The schema follows EVOLUTION_SOURCE_OF_TRUTH.md section 8.
"""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------------
# Event type registry
# --------------------------------------------------------------------------


class EventType(str, Enum):
    """Every event family required by the source of truth, section 8."""

    # experiment lifecycle
    EXPERIMENT_CREATED = "experiment.created"
    EXPERIMENT_STARTED = "experiment.started"
    EXPERIMENT_PAUSED = "experiment.paused"
    EXPERIMENT_RESUMED = "experiment.resumed"
    EXPERIMENT_STOPPED = "experiment.stopped"
    EXPERIMENT_COMPLETED = "experiment.completed"
    EXPERIMENT_FAILED = "experiment.failed"

    # generation
    GENERATION_STARTED = "generation.started"
    GENERATION_COMPLETED = "generation.completed"

    # candidate
    CANDIDATE_SAMPLED = "candidate.sampled"
    CANDIDATE_CREATED = "candidate.created"
    CANDIDATE_MUTATION_STARTED = "candidate.mutation.started"
    CANDIDATE_MUTATION_COMPLETED = "candidate.mutation.completed"
    CANDIDATE_REALIZATION_STARTED = "candidate.realization.started"
    CANDIDATE_REALIZATION_COMPLETED = "candidate.realization.completed"
    CANDIDATE_EVALUATION_STARTED = "candidate.evaluation.started"
    CANDIDATE_EVALUATION_COMPLETED = "candidate.evaluation.completed"
    CANDIDATE_REJECTED = "candidate.rejected"
    CANDIDATE_PROMOTED = "candidate.promoted"
    CANDIDATE_BEST_UPDATED = "candidate.best.updated"

    # verification (V1): is the improvement real?
    CANDIDATE_VERIFICATION_STARTED = "candidate.verification.started"
    CANDIDATE_VERIFICATION_PASSED = "candidate.verification.passed"
    CANDIDATE_VERIFICATION_FAILED = "candidate.verification.failed"
    CANDIDATE_SUSPICIOUS = "candidate.suspicious"

    # population / archive
    POPULATION_UPDATED = "population.updated"
    ARCHIVE_UPDATED = "archive.updated"

    # islands
    ISLAND_CREATED = "island.created"
    ISLAND_UPDATED = "island.updated"
    ISLAND_MIGRATION_STARTED = "island.migration.started"
    ISLAND_MIGRATION_COMPLETED = "island.migration.completed"

    # MAP-Elites
    MAP_ELITES_CELL_UPDATED = "map_elites.cell.updated"
    MAP_ELITES_ELITE_REPLACED = "map_elites.elite.replaced"

    # model calls
    MODEL_REQUEST_STARTED = "model.request.started"
    MODEL_REQUEST_COMPLETED = "model.request.completed"
    MODEL_REQUEST_FAILED = "model.request.failed"
    MODEL_RATE_LIMITED = "model.rate_limited"
    MODEL_RETRY = "model.retry"

    # evaluator
    EVALUATOR_STARTED = "evaluator.started"
    EVALUATOR_METRIC = "evaluator.metric"
    EVALUATOR_STDOUT = "evaluator.stdout"
    EVALUATOR_STDERR = "evaluator.stderr"
    EVALUATOR_COMPLETED = "evaluator.completed"
    EVALUATOR_FAILED = "evaluator.failed"

    # sandbox
    SANDBOX_CREATED = "sandbox.created"
    SANDBOX_STARTED = "sandbox.started"
    SANDBOX_COMMAND = "sandbox.command"
    SANDBOX_PROCESS = "sandbox.process"
    SANDBOX_NETWORK = "sandbox.network"
    SANDBOX_ARTIFACT = "sandbox.artifact"
    SANDBOX_COMPLETED = "sandbox.completed"
    SANDBOX_DESTROYED = "sandbox.destroyed"

    # OpenCode
    OPENCODE_SESSION_STARTED = "opencode.session.started"
    OPENCODE_AGENT_STARTED = "opencode.agent.started"
    OPENCODE_AGENT_COMPLETED = "opencode.agent.completed"
    OPENCODE_TOOL_CALLED = "opencode.tool.called"
    OPENCODE_TOOL_COMPLETED = "opencode.tool.completed"

    # Oh My OpenAgent
    OMO_MODE_ACTIVATED = "omo.mode.activated"
    OMO_TEAM_CREATED = "omo.team.created"
    OMO_MEMBER_STARTED = "omo.member.started"
    OMO_MEMBER_COMPLETED = "omo.member.completed"

    # checkpoints
    CHECKPOINT_CREATED = "checkpoint.created"
    CHECKPOINT_LOADED = "checkpoint.loaded"
    CHECKPOINT_FAILED = "checkpoint.failed"

    # resources
    RESOURCE_CPU = "resource.cpu"
    RESOURCE_RAM = "resource.ram"
    RESOURCE_GPU = "resource.gpu"
    RESOURCE_VRAM = "resource.vram"
    RESOURCE_DISK = "resource.disk"
    RESOURCE_NETWORK = "resource.network"

    # system / self-health
    TELEMETRY_HEALTH = "telemetry.health"
    SYSTEM_HEALTH = "system.health"
    SYSTEM_WARNING = "system.warning"
    SYSTEM_ERROR = "system.error"

    # control-plane commands (audit trail)
    CONTROL_COMMAND = "control.command"


class Status(str, Enum):
    OK = "ok"
    RUNNING = "running"
    FAILED = "failed"
    REJECTED = "rejected"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    WARNING = "warning"


class Component(str, Enum):
    """Which subsystem produced the event."""

    ENGINE = "engine"
    DATABASE = "database"
    EVALUATOR = "evaluator"
    VERIFIER = "verifier"
    LLM = "llm"
    CONTROLLER = "controller"
    SANDBOX = "sandbox"
    OPENCODE = "opencode"
    OMO = "omo"
    PROVIDER = "provider"
    CONTROL_PLANE = "control_plane"
    TELEMETRY = "telemetry"
    RESOURCE = "resource"


# Event families that are high-volume and may be sampled under load.
# Kept explicit so sampling can never silently drop a decision-carrying event.
SAMPLEABLE = frozenset(
    {
        EventType.EVALUATOR_STDOUT,
        EventType.EVALUATOR_STDERR,
        EventType.RESOURCE_CPU,
        EventType.RESOURCE_RAM,
        EventType.RESOURCE_GPU,
        EventType.RESOURCE_VRAM,
        EventType.RESOURCE_DISK,
        EventType.RESOURCE_NETWORK,
        EventType.SANDBOX_PROCESS,
        EventType.OPENCODE_TOOL_CALLED,
    }
)


def new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex
    return f"{prefix}{raw}" if prefix else raw


# --------------------------------------------------------------------------
# Event
# --------------------------------------------------------------------------


@dataclass
class Event:
    """
    A single telemetry event.

    Correlation model:
      trace_id       groups everything belonging to one logical run
      span_id        this unit of work
      parent_span_id enclosing unit of work

    All identity fields are optional because event families legitimately differ
    (a resource sample has no candidate; a migration has no model call).
    """

    type: EventType
    component: Component

    event_id: str = field(default_factory=lambda: new_id("ev_"))
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    parent_span_id: Optional[str] = None

    experiment_id: Optional[str] = None
    run_id: Optional[str] = None
    generation: Optional[int] = None
    iteration: Optional[int] = None
    candidate_id: Optional[str] = None
    parent_candidate_ids: List[str] = field(default_factory=list)
    island_id: Optional[int] = None

    timestamp: float = field(default_factory=time.time)
    duration_ms: Optional[float] = None

    status: Status = Status.OK
    summary: str = ""

    input: Dict[str, Any] = field(default_factory=dict)
    output: Dict[str, Any] = field(default_factory=dict)
    metrics: Dict[str, float] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[Dict[str, Any]] = None

    # Set by the emitting process so multi-process runs stay attributable.
    pid: int = field(default_factory=os.getpid)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["type"] = self.type.value
        d["component"] = self.component.value
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Event":
        d = dict(d)
        d["type"] = EventType(d["type"])
        d["component"] = Component(d["component"])
        d["status"] = Status(d.get("status", "ok"))
        # Drop unknown keys rather than raising: a newer emitter must not break
        # an older reader during a rolling upgrade.
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def is_sampleable(self) -> bool:
        return self.type in SAMPLEABLE
