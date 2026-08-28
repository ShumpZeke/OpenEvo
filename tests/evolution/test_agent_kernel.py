import pytest
from threading import Barrier

import control_plane.agent as agent_module
from control_plane.agent import (
    AgentKernel,
    AgentTask,
    ExecutionWorld,
    Goal,
    ToolCall,
    ToolResult,
    native_world_tools,
)


class DeliberateTestError(Exception):
    pass


class ExplodingTool:
    name = "explode"

    def invoke(self, call: ToolCall) -> ToolResult:
        raise DeliberateTestError(call.name)


def test_kernel_runs_dependency_dag_and_records_events():
    kernel = AgentKernel()
    first = AgentTask("inspect")
    second = AgentTask("verify", dependencies=(first.task_id,))
    result = kernel.run(Goal("improve"), (first, second), lambda task: task.objective)
    assert [task.output for task in result] == ["inspect", "verify"]
    assert all(task.status.value == "completed" for task in result)
    assert len(kernel.events()) == 6
    assert (
        sum(event.type.value == "agent.task.completed" for event in kernel.events())
        == 2
    )


def test_kernel_marks_unresolved_dependency_blocked():
    kernel = AgentKernel()
    task = AgentTask("wait", dependencies=("missing",))
    result = kernel.run(Goal("blocked"), (task,), lambda item: item.objective)
    assert result[0].status.value == "blocked"


def test_execution_world_tools_share_one_candidate_root(tmp_path):
    world = ExecutionWorld(str(tmp_path))
    kernel = AgentKernel()
    for tool in native_world_tools(world):
        kernel.register_tool(tool)
    written = kernel.tool(
        ToolCall("write_file", {"path": "src/value.txt", "content": "42"})
    )
    read = kernel.tool(ToolCall("read_file", {"path": "src/value.txt"}))
    command = kernel.tool(
        ToolCall(
            "shell", {"command": "python -c \"print(open('src/value.txt').read())\""}
        )
    )
    assert written.succeeded
    assert read.output == "42"
    assert command.succeeded
    assert "42" in command.output


def test_execution_world_rejects_escape(tmp_path):
    world = ExecutionWorld(str(tmp_path))
    result = next(
        tool for tool in native_world_tools(world) if tool.name == "read_file"
    ).invoke(ToolCall("read_file", {"path": "../outside.txt"}))
    assert not result.succeeded
    assert result.error == "ValueError"

    with pytest.raises(agent_module.WorkspaceEscapeError):
        world.path("../outside.txt")


def test_native_search_glob_and_metadata_tools(tmp_path):
    world = ExecutionWorld(str(tmp_path))
    kernel = AgentKernel()
    for tool in native_world_tools(world):
        kernel.register_tool(tool)
    kernel.tool(
        ToolCall("write_file", {"path": "src/main.py", "content": "answer = 7\n"})
    )
    assert "src/main.py" in kernel.tool(ToolCall("glob", {"pattern": "**/*.py"})).output
    assert "answer" in kernel.tool(ToolCall("search_text", {"query": "answer"})).output
    assert kernel.tool(ToolCall("file_metadata", {"path": "src/main.py"})).succeeded


def test_native_git_tools_are_bound_to_candidate_world(tmp_path):
    world = ExecutionWorld(str(tmp_path))
    kernel = AgentKernel()
    for tool in native_world_tools(world):
        kernel.register_tool(tool)
    result = kernel.tool(ToolCall("git_status --short"))
    assert not result.succeeded
    assert result.tool == "git_status --short"


def test_kernel_converts_tool_crash_to_structured_failure():
    kernel = AgentKernel()
    kernel.register_tool(ExplodingTool())

    result = kernel.tool(ToolCall("explode"))

    assert not result.succeeded
    assert result.error == "DeliberateTestError"


def test_kernel_converts_runner_crash_to_failed_task():
    kernel = AgentKernel()
    task = AgentTask("crash")

    def crash(_task: AgentTask) -> str:
        raise DeliberateTestError("runner failed")

    result = kernel.run(Goal("recover"), (task,), crash)

    assert result[0].status.value == "failed"
    assert result[0].error == "DeliberateTestError"


def test_kernel_rejects_duplicate_tool_name():
    kernel = AgentKernel()
    kernel.register_tool(ExplodingTool())

    with pytest.raises(ValueError, match="tool name is unavailable"):
        kernel.register_tool(ExplodingTool())


def test_kernel_rejects_duplicate_task_ids():
    task = AgentTask("same")

    with pytest.raises(ValueError, match="task ids must be unique"):
        AgentKernel().run(Goal("unique"), (task, task), lambda item: item.objective)


def test_kernel_uses_dynamic_role_assignment_before_running_task():
    planner = agent_module.DelegationPlanner(
        (
            agent_module.RoleProfile("researcher", frozenset({"search"}), priority=10),
            agent_module.RoleProfile("implementer", frozenset({"code"}), priority=5),
        )
    )
    task = AgentTask("implement", required_capabilities=("code",))

    result = AgentKernel(delegator=planner).run(
        Goal("delegate"),
        (task,),
        lambda item: item.assigned_agent or "missing",
    )

    assert result[0].assigned_agent == "implementer"
    assert result[0].output == "implementer"


def test_kernel_blocks_task_when_no_role_has_required_capabilities():
    planner = agent_module.DelegationPlanner(
        (agent_module.RoleProfile("researcher", frozenset({"search"})),)
    )
    task = AgentTask("implement", required_capabilities=("code",))

    result = AgentKernel(delegator=planner).run(
        Goal("delegate"),
        (task,),
        lambda item: item.objective,
    )

    assert result[0].status.value == "blocked"
    assert result[0].error == "NoEligibleRoleError"


def test_kernel_raises_typed_registration_and_task_identity_errors():
    kernel = AgentKernel()
    kernel.register_tool(ExplodingTool())

    with pytest.raises(agent_module.ToolRegistrationError):
        kernel.register_tool(ExplodingTool())

    task = AgentTask("same")
    with pytest.raises(agent_module.DuplicateTaskIdError):
        kernel.run(Goal("unique"), (task, task), lambda item: item.objective)


def test_kernel_runs_independent_roles_concurrently():
    planner = agent_module.DelegationPlanner(
        (
            agent_module.RoleProfile("researcher", frozenset({"search"})),
            agent_module.RoleProfile("implementer", frozenset({"code"})),
        )
    )
    tasks = (
        AgentTask("inspect", required_capabilities=("search",)),
        AgentTask("change", required_capabilities=("code",)),
    )
    rendezvous = Barrier(2)

    def runner(task: AgentTask) -> str:
        rendezvous.wait(timeout=2)
        return task.assigned_agent or "missing"

    result = AgentKernel(delegator=planner).run(
        Goal("parallel roles"),
        tasks,
        runner,
        max_workers=2,
    )

    assert {task.output for task in result} == {"researcher", "implementer"}
    assert all(task.status.value == "completed" for task in result)
