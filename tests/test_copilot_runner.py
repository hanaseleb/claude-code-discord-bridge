"""Tests for the GitHub Copilot SDK backend."""

from __future__ import annotations

import asyncio
import sys
from types import ModuleType, SimpleNamespace

import pytest

from claude_code_core.backend import SessionBackend
from claude_code_core.copilot_runner import (
    CopilotEventState,
    CopilotRunner,
    convert_copilot_event,
)
from claude_code_core.types import MessageType, ToolCategory


def _event(event_type: str, **data: object) -> SimpleNamespace:
    return SimpleNamespace(
        type=SimpleNamespace(value=event_type),
        data=SimpleNamespace(**data),
    )


class TestCopilotEventConversion:
    def test_session_start_captures_session_id(self) -> None:
        state = CopilotEventState()

        event = convert_copilot_event(
            _event("session.start", session_id="copilot-session-1"),
            state,
        )

        assert event is not None
        assert event.message_type is MessageType.SYSTEM
        assert event.session_id == "copilot-session-1"

    def test_streaming_deltas_are_accumulated_for_event_processor(self) -> None:
        state = CopilotEventState()

        first = convert_copilot_event(
            _event("assistant.message_delta", message_id="m1", delta_content="Hello"),
            state,
        )
        second = convert_copilot_event(
            _event("assistant.message_delta", message_id="m1", delta_content=" world"),
            state,
        )

        assert first is not None and first.text == "Hello"
        assert second is not None and second.text == "Hello world"
        assert first.is_partial is True
        assert second.is_partial is True

    def test_complete_message_is_emitted_when_no_deltas_were_seen(self) -> None:
        state = CopilotEventState()

        event = convert_copilot_event(
            _event("assistant.message", message_id="m1", content="Finished"),
            state,
        )

        assert event is not None
        assert event.message_type is MessageType.ASSISTANT
        assert event.text == "Finished"
        assert event.is_partial is False

    def test_complete_message_after_deltas_flushes_without_duplicate_text(self) -> None:
        state = CopilotEventState()
        convert_copilot_event(
            _event("assistant.message_delta", message_id="m1", delta_content="Finished"),
            state,
        )

        event = convert_copilot_event(
            _event("assistant.message", message_id="m1", content="Finished"),
            state,
        )

        assert event is not None
        assert event.text == "Finished"
        assert event.is_partial is False

    def test_tool_start_maps_shell_to_bash(self) -> None:
        state = CopilotEventState()

        event = convert_copilot_event(
            _event(
                "tool.execution_start",
                tool_call_id="tool-1",
                tool_name="shell",
                arguments={"command": "git status"},
            ),
            state,
        )

        assert event is not None and event.tool_use is not None
        assert event.tool_use.tool_name == "Bash"
        assert event.tool_use.category is ToolCategory.COMMAND
        assert event.tool_use.tool_input == {"command": "git status"}

    def test_tool_complete_maps_result(self) -> None:
        state = CopilotEventState()

        event = convert_copilot_event(
            _event(
                "tool.execution_complete",
                tool_call_id="tool-1",
                success=True,
                result=SimpleNamespace(content="clean"),
                error=None,
            ),
            state,
        )

        assert event is not None
        assert event.message_type is MessageType.USER
        assert event.tool_result_id == "tool-1"
        assert event.tool_result_content == "clean"

    def test_usage_is_carried_to_idle_completion(self) -> None:
        state = CopilotEventState()
        assert (
            convert_copilot_event(
                _event(
                    "assistant.usage",
                    model="gpt-5.4",
                    input_tokens=120,
                    output_tokens=30,
                    cache_read_tokens=20,
                    cache_write_tokens=5,
                    cost=0.012,
                    duration=SimpleNamespace(total_seconds=lambda: 1.5),
                ),
                state,
            )
            is None
        )

        event = convert_copilot_event(_event("session.idle", aborted=False), state)

        assert event is not None and event.is_complete is True
        assert event.input_tokens == 120
        assert event.output_tokens == 30
        assert event.cache_read_tokens == 20
        assert event.cache_creation_tokens == 5
        assert event.cost_usd == 0.012
        assert event.duration_ms == 1500

    def test_session_error_becomes_terminal_result(self) -> None:
        event = convert_copilot_event(
            _event("session.error", error_type="authentication", message="Please log in"),
            CopilotEventState(),
        )

        assert event is not None and event.is_complete is True
        assert event.error == "authentication: Please log in"


class TestCopilotRunner:
    def test_satisfies_backend_protocol(self) -> None:
        runner = CopilotRunner(model="auto")
        assert isinstance(runner, SessionBackend)

    def test_clone_preserves_configuration(self) -> None:
        runner = CopilotRunner(
            model="gpt-5.4",
            working_dir="/tmp/project",
            effort="high",
            agent_mode="autopilot",
            thread_id=42,
        )

        clone = runner.clone(model="claude-sonnet-4.6")

        assert isinstance(clone, CopilotRunner)
        assert clone.model == "claude-sonnet-4.6"
        assert clone.working_dir == "/tmp/project"
        assert clone.effort == "high"
        assert clone.agent_mode == "autopilot"
        assert clone.thread_id == 42

    def test_build_env_injects_ccdb_context_and_strips_discord_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "secret")
        runner = CopilotRunner(api_port=8099, api_secret="api-secret", thread_id=42)

        env = runner._build_env()

        assert "DISCORD_BOT_TOKEN" not in env
        assert env["CCDB_API_URL"] == "http://127.0.0.1:8099"
        assert env["CCDB_API_SECRET"] == "api-secret"
        assert env["DISCORD_THREAD_ID"] == "42"

    async def test_rejects_invalid_resume_session_id_before_sdk_start(self) -> None:
        events = [
            event
            async for event in CopilotRunner().run(
                "hello",
                session_id="../../attacker-controlled",
            )
        ]

        assert len(events) == 1
        assert events[0].is_complete is True
        assert events[0].error == "Invalid GitHub Copilot session ID"

    async def test_rejects_invalid_agent_mode_before_sdk_start(self) -> None:
        events = [event async for event in CopilotRunner(agent_mode="reckless").run("hello")]

        assert len(events) == 1
        assert events[0].is_complete is True
        assert events[0].error == (
            "Invalid Copilot agent mode 'reckless'; choose one of autopilot, interactive"
        )

    async def test_run_streams_sdk_session_events(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clients: list[object] = []

        class FakeSession:
            def __init__(self, options: dict[str, object]) -> None:
                self.options = options
                self.disconnected = False
                self.agent_mode: str | None = None

            async def send_and_wait(self, prompt: str, **kwargs: object) -> None:
                assert prompt == "hello"
                self.agent_mode = kwargs.get("agent_mode")  # type: ignore[assignment]
                on_event = self.options["on_event"]
                assert callable(on_event)
                on_event(_event("assistant.message", message_id="m1", content="Hi"))
                on_event(_event("session.idle", aborted=False))

            async def disconnect(self) -> None:
                self.disconnected = True

            async def abort(self) -> None:
                return None

        class FakeClient:
            def __init__(self, **_kwargs: object) -> None:
                clients.append(self)
                self.started = False
                self.stopped = False
                self.session: FakeSession | None = None
                self.session_id: str | None = None

            async def start(self) -> None:
                self.started = True

            async def create_session(
                self,
                *,
                session_id: str,
                **options: object,
            ) -> FakeSession:
                self.session_id = session_id
                self.session = FakeSession(options)
                return self.session

            async def stop(self) -> None:
                self.stopped = True

        class FakeConnection:
            def __init__(self, **_kwargs: object) -> None:
                pass

        class FakeApprove:
            pass

        class FakeReject:
            pass

        copilot_module = ModuleType("copilot")
        copilot_module.CopilotClient = FakeClient  # type: ignore[attr-defined]
        copilot_module.ChildProcessRuntimeConnection = FakeConnection  # type: ignore[attr-defined]
        generated_module = ModuleType("copilot.generated")
        rpc_module = ModuleType("copilot.generated.rpc")
        rpc_module.PermissionDecisionApproveOnce = FakeApprove  # type: ignore[attr-defined]
        rpc_module.PermissionDecisionReject = FakeReject  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "copilot", copilot_module)
        monkeypatch.setitem(sys.modules, "copilot.generated", generated_module)
        monkeypatch.setitem(sys.modules, "copilot.generated.rpc", rpc_module)

        events = [event async for event in CopilotRunner(agent_mode="autopilot").run("hello")]

        assert [event.message_type for event in events] == [
            MessageType.SYSTEM,
            MessageType.ASSISTANT,
            MessageType.RESULT,
        ]
        assert events[0].session_id is not None
        assert events[1].text == "Hi"
        assert events[2].is_complete is True
        assert len(clients) == 1
        client = clients[0]
        assert isinstance(client, FakeClient)
        assert client.started is True
        assert client.stopped is True
        assert client.session is not None and client.session.disconnected is True
        assert client.session.agent_mode == "autopilot"

    async def test_permission_waiter_is_resolved_by_discord_injection(self) -> None:
        runner = CopilotRunner()
        runner._event_queue = asyncio.Queue()
        request = SimpleNamespace(
            kind="shell",
            full_command_text="pytest",
            intention="Run tests",
        )

        waiter = asyncio.create_task(runner._request_permission(request))
        event = await asyncio.wait_for(runner._event_queue.get(), timeout=1)
        assert event is not None and event.permission_request is not None

        await runner.inject_tool_result(
            event.permission_request.request_id,
            {"approved": True},
        )

        assert await asyncio.wait_for(waiter, timeout=1) is True

    async def test_yolo_permission_is_approved_without_discord_event(self) -> None:
        runner = CopilotRunner(dangerously_skip_permissions=True)
        runner._event_queue = asyncio.Queue()

        approved = await runner._request_permission(SimpleNamespace(kind="write"))

        assert approved is True
        assert runner._event_queue.empty()
