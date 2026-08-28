import pytest

from control_plane.agent import (
    AgentTask,
    ExecutionWorld,
    Goal,
    NativeAgentRuntime,
    ToolCall,
    native_world_tools,
    EventLog,
    KnowledgeItem,
    LineageMemory,
    GoalStore,
    PythonCodeIndex,
    Experiment,
    ExperimentEngine,
)
from control_plane.telemetry.events import Component, Event, EventType
from openevolve.controller import OpenEvolve
import control_plane.agent as agent_module


@pytest.fixture(autouse=True)
def _install_native_agent():
    """
    Attach the native-agent methods to `OpenEvolve` at runtime.

    Upstream ships no such methods, and this fork does not add them by editing
    `openevolve/controller.py` — that would end the byte-identical guarantee
    rule 1 exists to protect. `control_plane.native.install()` binds them to the
    class instead, which is the same pattern `telemetry/instrument.py` uses.
    """
    from control_plane.native import install

    install()




def test_native_runtime_modifies_and_independently_evaluates_candidate(tmp_path):
    runtime = NativeAgentRuntime(ExecutionWorld(str(tmp_path)))
    for tool in native_world_tools(runtime.world):
        runtime.register_tool(tool)

    def runner(task):
        result = runtime.kernel.tool(
            ToolCall("write_file", {"path": "answer.txt", "content": "7"})
        )
        assert result.succeeded
        return task.objective

    candidate = runtime.run(
        Goal("write answer"),
        (AgentTask("implement"),),
        runner,
        lambda world: {"correctness": float(world.read("answer.txt") == "7")},
        lambda metrics: metrics["correctness"] == 1.0,
    )
    assert candidate.accepted
    assert candidate.metrics["correctness"] == 1.0
    assert "independent_evaluator" in candidate.evidence


def test_event_log_replays_typed_events(tmp_path):
    log = EventLog(str(tmp_path / "events.ndjson"))
    original = Event(
        EventType.CONTROL_COMMAND,
        Component.CONTROL_PLANE,
        summary="checkpoint",
        metadata={"step": 1},
    )
    log.append(original)
    replayed = tuple(log.replay())
    assert len(replayed) == 1
    assert replayed[0].type is EventType.CONTROL_COMMAND
    assert replayed[0].metadata == {"step": 1}


def test_lineage_memory_inherits_distilled_knowledge(tmp_path):
    memory = LineageMemory(str(tmp_path / "memory.ndjson"))
    memory.remember(KnowledgeItem("hypothesis", "vectorize loop", "parent-a", 0.8))
    memory.remember(KnowledgeItem("failure", "overflow at n=0", "parent-a"))
    memory.remember(KnowledgeItem("hypothesis", "vectorize loop", "parent-b", 0.9))
    inherited = memory.inherit(["parent-a"])
    assert [item.kind for item in inherited] == ["hypothesis", "failure"]
    assert memory.inherit(["unknown"]) == ()


def test_runtime_attaches_parent_knowledge_to_candidate(tmp_path):
    memory = LineageMemory(str(tmp_path / "memory.ndjson"))
    memory.remember(KnowledgeItem("discovery", "cache helps", "parent"))
    runtime = NativeAgentRuntime(ExecutionWorld(str(tmp_path / "world")), memory=memory)
    candidate = runtime.run(
        Goal("descendant"),
        (AgentTask("continue"),),
        lambda task: task.objective,
        lambda world: {"score": 1.0},
        lambda metrics: True,
        parent_ids=("parent",),
    )
    assert candidate.inherited_knowledge[0].statement == "cache helps"


def test_controller_exposes_native_agent_workflow(tmp_path):
    controller = OpenEvolve.__new__(OpenEvolve)
    controller.output_dir = str(tmp_path)
    goal = Goal("controller workflow")
    candidate = controller.run_native_agent(
        goal,
        (AgentTask("write"),),
        lambda task: task.objective,
        lambda world: {"score": 1.0},
        lambda metrics: metrics["score"] == 1.0,
    )
    assert candidate.accepted
    assert (tmp_path / "agent_events.ndjson").exists()
    assert (tmp_path / "agent_goals.ndjson").exists()


def test_goal_store_resumes_persisted_goal(tmp_path):
    store = GoalStore(str(tmp_path / "goals.ndjson"))
    created = store.create(Goal("resume me"), ["task-1"])
    resumed = GoalStore(str(tmp_path / "goals.ndjson")).get(created.goal.goal_id)
    assert resumed is not None
    assert resumed.goal.objective == "resume me"
    assert resumed.task_ids == ("task-1",)


def test_python_code_index_finds_symbols_references_and_refreshes(tmp_path):
    source = tmp_path / "module.py"
    source.write_text(
        "def answer():\n    return 7\nvalue = answer()\n", encoding="utf-8"
    )
    index = PythonCodeIndex(str(tmp_path))
    assert index.symbols("answer")[0].kind == "FunctionDef"
    assert len(index.references("answer")) == 1
    source.write_text("class Answer:\n    pass\n", encoding="utf-8")
    assert index.symbols("Answer")[0].kind == "ClassDef"
    assert index.symbols("answer") == ()


def test_experiment_engine_rejects_noise_as_improvement():
    calls = {"baseline": 0, "candidate": 0}
    baseline_values = iter((10.0, 10.0, 10.0))
    candidate_values = iter((9.0, 11.0, 10.0))
    result = ExperimentEngine().run(
        Experiment("candidate is faster", repetitions=3),
        lambda: next(baseline_values),
        lambda: next(candidate_values),
    )
    assert result.absolute_delta == 0.0
    assert not result.accepted
    assert result.evidence == ("repeated_trials", "variance_checked")


def test_experiment_engine_raises_typed_error_for_single_trial():
    with pytest.raises(agent_module.InvalidExperimentError):
        ExperimentEngine().run(
            Experiment("too few samples", repetitions=1), lambda: 1.0, lambda: 2.0
        )


def test_delegation_assigns_best_role_when_capabilities_differ():
    planner = agent_module.DelegationPlanner(
        (
            agent_module.RoleProfile(
                "researcher", frozenset({"web", "search"}), priority=10
            ),
            agent_module.RoleProfile(
                "implementer", frozenset({"code", "test"}), priority=5
            ),
        )
    )
    task = AgentTask("repair parser", required_capabilities=("code", "test"))
    assignment = planner.assign(task)
    assert assignment.role == "implementer"
