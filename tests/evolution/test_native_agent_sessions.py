
import pytest
from pathlib import Path

from control_plane.agent import (
    AgentRunStatus,
    AgentSessionStore,
    ExecutionWorld,
    Goal,
    MessageRole,
    ModelRequest,
    ModelResponse,
    ModelToolArgument,
    ModelToolCall,
    NativeAgentRuntime,
    RoutedModelRuntime,
    native_world_tool_definitions,
    native_world_tools,
)
from control_plane.providers.profiles import Capability, ModelProfile, Role
from control_plane.providers.router import ModelRouter
from openevolve.controller import OpenEvolve


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




class _ResumableProvider:
    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    def complete(self, profile: ModelProfile, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        tool_messages = tuple(
            message for message in request.messages if message.role is MessageRole.TOOL
        )
        if len(tool_messages) < 2:
            number = len(tool_messages) + 1
            return ModelResponse(
                content="",
                finish_reason="tool_calls",
                tool_calls=(
                    ModelToolCall(
                        f"write-{number}",
                        "write_file",
                        (
                            ModelToolArgument("path", f"{number}.txt"),
                            ModelToolArgument("content", str(number)),
                        ),
                    ),
                ),
            )
        return ModelResponse(content="resumed and finished", finish_reason="stop")


class _ForkProvider:
    def complete(self, profile: ModelProfile, request: ModelRequest) -> ModelResponse:
        return ModelResponse(content="fork diverged", finish_reason="stop")


def _runtime(root: Path, store: AgentSessionStore) -> NativeAgentRuntime:
    runtime = NativeAgentRuntime(ExecutionWorld(str(root)), sessions=store)
    for tool in native_world_tools(runtime.world):
        runtime.register_tool(tool)
    return runtime


def _routed(provider: _ResumableProvider | _ForkProvider) -> RoutedModelRuntime:
    profile = ModelProfile(
        id="session-test",
        provider="local",
        model="session-test-model",
        api_base="http://local/v1",
        declared_capabilities=[Capability.CHAT, Capability.TOOLS],
        roles=[Role.ORCHESTRATOR],
        requires_key=False,
    )
    return RoutedModelRuntime(
        ModelRouter(
            profiles=[profile],
            role_chains={Role.ORCHESTRATOR: [profile.id]},
        ),
        provider,
    )


def test_session_resumes_from_durable_model_and_tool_history(tmp_path: Path) -> None:
    sessions = AgentSessionStore(str(tmp_path / "sessions"))
    goal = Goal("write two files")
    first_provider = _ResumableProvider()
    first = _runtime(tmp_path / "world", sessions).run_model_goal(
        goal,
        _routed(first_provider),
        native_world_tool_definitions(),
        max_steps=1,
        max_tool_calls=4,
    )

    assert first.status is AgentRunStatus.STEP_LIMIT
    assert first.session_id
    assert (tmp_path / "world" / "1.txt").read_text(encoding="utf-8") == "1"

    resumed_provider = _ResumableProvider()
    resumed = _runtime(tmp_path / "world", sessions).run_model_goal(
        goal,
        _routed(resumed_provider),
        native_world_tool_definitions(),
        max_steps=3,
        max_tool_calls=4,
        session_id=first.session_id,
    )

    assert resumed.status is AgentRunStatus.COMPLETED
    assert resumed.output == "resumed and finished"
    assert resumed.steps == 3
    assert len(resumed.tool_results) == 2
    assert resumed_provider.requests[0].messages[-1].role is MessageRole.TOOL
    assert (tmp_path / "world" / "2.txt").read_text(encoding="utf-8") == "2"


def test_session_fork_inherits_history_then_diverges_independently(
    tmp_path: Path,
) -> None:
    sessions = AgentSessionStore(str(tmp_path / "sessions"))
    goal = Goal("fork this work")
    parent = _runtime(tmp_path / "world", sessions).run_model_goal(
        goal,
        _routed(_ResumableProvider()),
        native_world_tool_definitions(),
        max_steps=1,
    )
    fork = sessions.fork(parent.session_id)

    child = _runtime(tmp_path / "world", sessions).run_model_goal(
        goal,
        _routed(_ForkProvider()),
        native_world_tool_definitions(),
        max_steps=2,
        session_id=str(fork.session_id),
    )

    assert child.status is AgentRunStatus.COMPLETED
    assert child.output == "fork diverged"
    assert child.session_id != parent.session_id
    assert child.parent_session_id == parent.session_id
    assert sessions.load(parent.session_id).status is AgentRunStatus.STEP_LIMIT
    assert sessions.load(child.session_id).status is AgentRunStatus.COMPLETED


def test_session_persistence_redacts_credentials(tmp_path: Path) -> None:
    sessions = AgentSessionStore(str(tmp_path / "sessions"))
    synthetic_secret = "nvapi-" + "A" * 24
    result = _runtime(tmp_path / "world", sessions).run_model_goal(
        Goal(f"Never persist {synthetic_secret}"),
        _routed(_ForkProvider()),
        native_world_tool_definitions(),
    )

    stored = (tmp_path / "sessions" / f"{result.session_id}.json").read_text(
        encoding="utf-8"
    )
    assert synthetic_secret not in stored
    assert "redacted" in stored


def test_controller_forks_persisted_native_model_session(tmp_path: Path) -> None:
    controller = OpenEvolve.__new__(OpenEvolve)
    controller.output_dir = str(tmp_path)
    parent = controller.run_native_model_agent(
        Goal("controller session"),
        model_runtime=_routed(_ForkProvider()),
    )

    child = controller.fork_native_model_session(parent.session_id)

    assert str(child.session_id) != parent.session_id
    assert str(child.parent_session_id) == parent.session_id
