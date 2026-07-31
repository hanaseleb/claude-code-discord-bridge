"""GitHub Copilot SDK backend.

The SDK owns the Copilot CLI subprocess and exposes typed session events.  This
runner adapts those events to ccdb's backend-neutral ``StreamEvent`` protocol so
the Discord UI, session persistence, Lounge, permissions, and stop button work
the same way as the Claude and Codex backends.

``github-copilot-sdk`` is intentionally imported lazily. It is installed on
Python 3.11+, while ccdb's Claude/Codex-only runtime remains importable on
Python 3.10.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import re
import uuid
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from .types import (
    ImageData,
    MessageType,
    PermissionRequest,
    StreamEvent,
    ToolCategory,
    ToolUseEvent,
)

logger = logging.getLogger(__name__)

_UNSET = object()
VALID_COPILOT_EFFORTS: frozenset[str] = frozenset({"low", "medium", "high", "xhigh"})
VALID_COPILOT_AGENT_MODES: frozenset[str] = frozenset({"interactive", "autopilot"})
CopilotAgentMode = Literal["interactive", "autopilot"]
_SESSION_ID_RE = re.compile(r"^[a-f0-9-]+$")


@dataclass
class CopilotEventState:
    """Mutable conversion state shared by all events in one Copilot turn."""

    message_text: dict[str, str] = field(default_factory=dict)
    streamed_messages: set[str] = field(default_factory=set)
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    model: str | None = None


def _event_type(event: object) -> str:
    value = getattr(getattr(event, "type", ""), "value", getattr(event, "type", ""))
    return str(value)


def _object_dict(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return dataclasses.asdict(value)
    try:
        return dict(vars(value))
    except TypeError:
        return {"value": str(value)}


def _duration_ms(value: object) -> int | None:
    if value is None:
        return None
    total_seconds = getattr(value, "total_seconds", None)
    if callable(total_seconds):
        seconds = cast(Callable[[], float], total_seconds)()
        return int(seconds * 1000)
    if isinstance(value, (int, float)):
        return int(float(value) * 1000)
    return None


def _normalise_tool(name: str, arguments: dict[str, Any]) -> tuple[str, ToolCategory]:
    """Translate Copilot tool names to ccdb's established display vocabulary."""
    lowered = name.lower()
    if lowered in {"shell", "bash", "powershell"} or "shell" in lowered:
        return "Bash", ToolCategory.COMMAND
    if lowered in {"read", "view", "read_file"} or "read" in lowered or "view" in lowered:
        return "Read", ToolCategory.READ
    if lowered in {"write", "write_file"} or "write" in lowered:
        return "Write", ToolCategory.EDIT
    if lowered in {"edit", "edit_file", "apply_patch"} or "edit" in lowered:
        return "Edit", ToolCategory.EDIT
    if "grep" in lowered or "search" in lowered:
        return "Grep", ToolCategory.READ
    if "url" in lowered or "web" in lowered or "fetch" in lowered:
        return "WebFetch", ToolCategory.WEB
    if "task" in lowered or "agent" in lowered:
        return "Task", ToolCategory.OTHER
    return name, ToolCategory.OTHER


def convert_copilot_event(
    event: object,
    state: CopilotEventState,
) -> StreamEvent | None:
    """Convert one SDK session event to ccdb's frontend-neutral event type."""
    event_type = _event_type(event)
    data = getattr(event, "data", None)
    raw = {"type": event_type}

    if event_type in {"session.start", "session.resume"}:
        return StreamEvent(
            raw=raw,
            message_type=MessageType.SYSTEM,
            session_id=getattr(data, "session_id", None),
        )

    if event_type == "assistant.message_delta":
        message_id = str(getattr(data, "message_id", "default"))
        text = state.message_text.get(message_id, "") + str(getattr(data, "delta_content", ""))
        state.message_text[message_id] = text
        state.streamed_messages.add(message_id)
        return StreamEvent(
            raw=raw,
            message_type=MessageType.ASSISTANT,
            text=text,
            is_partial=True,
        )

    if event_type == "assistant.message":
        message_id = str(getattr(data, "message_id", "default"))
        content = str(getattr(data, "content", ""))
        state.message_text[message_id] = content
        return StreamEvent(
            raw=raw,
            message_type=MessageType.ASSISTANT,
            text=content,
            is_partial=False,
            output_tokens=getattr(data, "output_tokens", None),
        )

    if event_type == "tool.execution_start":
        arguments = _object_dict(getattr(data, "arguments", {}))
        tool_name, category = _normalise_tool(str(getattr(data, "tool_name", "Tool")), arguments)
        return StreamEvent(
            raw=raw,
            message_type=MessageType.ASSISTANT,
            tool_use=ToolUseEvent(
                tool_id=str(getattr(data, "tool_call_id", "")),
                tool_name=tool_name,
                tool_input=arguments,
                category=category,
            ),
        )

    if event_type == "tool.execution_complete":
        result = getattr(data, "result", None)
        error = getattr(data, "error", None)
        content = getattr(result, "content", "") if result is not None else ""
        if error is not None:
            content = getattr(error, "message", str(error))
        return StreamEvent(
            raw=raw,
            message_type=MessageType.USER,
            tool_result_id=str(getattr(data, "tool_call_id", "")),
            tool_result_content=str(content or ""),
        )

    if event_type == "assistant.usage":
        state.input_tokens = getattr(data, "input_tokens", None)
        state.output_tokens = getattr(data, "output_tokens", None)
        state.cache_read_tokens = getattr(data, "cache_read_tokens", None)
        state.cache_creation_tokens = getattr(data, "cache_write_tokens", None)
        state.cost_usd = getattr(data, "cost", None)
        state.duration_ms = _duration_ms(getattr(data, "duration", None))
        state.model = getattr(data, "model", None)
        return None

    if event_type == "session.error":
        error_type = str(getattr(data, "error_type", "error"))
        message = str(getattr(data, "message", "Unknown Copilot error"))
        return StreamEvent(
            raw=raw,
            message_type=MessageType.RESULT,
            is_complete=True,
            error=f"{error_type}: {message}",
        )

    if event_type == "session.idle":
        return StreamEvent(
            raw=raw,
            message_type=MessageType.RESULT,
            is_complete=True,
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
            cache_read_tokens=state.cache_read_tokens,
            cache_creation_tokens=state.cache_creation_tokens,
            cost_usd=state.cost_usd,
            duration_ms=state.duration_ms,
        )

    if event_type in {
        "assistant.intent",
        "assistant.turn_start",
        "tool.execution_progress",
        "tool.execution_partial_result",
    }:
        return StreamEvent(raw=raw, message_type=MessageType.PROGRESS)

    return None


class CopilotRunner:
    """Run GitHub Copilot through the official Python SDK."""

    backend_name = "copilot"

    def __init__(
        self,
        command: str = "copilot",
        model: str | None = "auto",
        permission_mode: str = "default",
        working_dir: str | None = None,
        timeout_seconds: int = 300,
        dangerously_skip_permissions: bool = False,
        allowed_tools: list[str] | None = None,
        api_port: int | None = None,
        api_secret: str | None = None,
        thread_id: int | None = None,
        append_system_prompt: str | None = None,
        images: list[ImageData] | None = None,
        effort: str | None = None,
        agent_mode: str = "interactive",
        **_kwargs: object,
    ) -> None:
        self.command = command
        self.model = model or "auto"
        self.permission_mode = permission_mode
        self.working_dir = working_dir
        self.timeout_seconds = timeout_seconds
        self.dangerously_skip_permissions = dangerously_skip_permissions
        self.allowed_tools = allowed_tools
        self.api_port = api_port
        self.api_secret = api_secret
        self.thread_id = thread_id
        self.append_system_prompt = append_system_prompt
        self.images = images
        self.effort = effort
        self.agent_mode = agent_mode
        self._client: Any | None = None
        self._session: Any | None = None
        self._event_queue: asyncio.Queue[StreamEvent | None] | None = None
        self._permission_waiters: dict[str, asyncio.Future[bool]] = {}
        self._stopping = False

    def clone(
        self,
        model: str | None = None,
        working_dir: str | None | object = _UNSET,
        thread_id: int | None = None,
        effort: str | None | object = _UNSET,
        append_system_prompt: str | None = None,
        **_kwargs: object,
    ) -> CopilotRunner:
        return CopilotRunner(
            command=self.command,
            model=model if model is not None else self.model,
            permission_mode=self.permission_mode,
            working_dir=(
                self.working_dir if working_dir is _UNSET else working_dir  # type: ignore[arg-type]
            ),
            timeout_seconds=self.timeout_seconds,
            dangerously_skip_permissions=self.dangerously_skip_permissions,
            allowed_tools=self.allowed_tools,
            api_port=self.api_port,
            api_secret=self.api_secret,
            thread_id=thread_id if thread_id is not None else self.thread_id,
            append_system_prompt=(
                append_system_prompt
                if append_system_prompt is not None
                else self.append_system_prompt
            ),
            images=self.images,
            effort=self.effort if effort is _UNSET else effort,  # type: ignore[arg-type]
            agent_mode=cast(CopilotAgentMode, self.agent_mode),
        )

    async def run(
        self,
        prompt: str,
        session_id: str | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Create/resume a Copilot session and yield converted SDK events."""
        if session_id is not None and not _SESSION_ID_RE.fullmatch(session_id):
            yield StreamEvent(
                raw={},
                message_type=MessageType.RESULT,
                is_complete=True,
                error="Invalid GitHub Copilot session ID",
            )
            return

        try:
            from copilot import (  # pyright: ignore[reportMissingImports]
                ChildProcessRuntimeConnection,
                CopilotClient,
            )
            from copilot.generated.rpc import (  # pyright: ignore[reportMissingImports]
                PermissionDecisionApproveOnce,
                PermissionDecisionReject,
            )
        except ImportError:
            yield StreamEvent(
                raw={},
                message_type=MessageType.RESULT,
                is_complete=True,
                error=(
                    "GitHub Copilot requires Python 3.11 or newer and the "
                    "github-copilot-sdk dependency. Upgrade the ccdb environment "
                    "(for example with `uv sync`) and try again."
                ),
            )
            return

        if self.effort and self.effort not in VALID_COPILOT_EFFORTS:
            yield StreamEvent(
                raw={},
                message_type=MessageType.RESULT,
                is_complete=True,
                error=(
                    f"Invalid Copilot effort {self.effort!r}; choose one of "
                    f"{', '.join(sorted(VALID_COPILOT_EFFORTS))}"
                ),
            )
            return

        if self.agent_mode not in VALID_COPILOT_AGENT_MODES:
            yield StreamEvent(
                raw={},
                message_type=MessageType.RESULT,
                is_complete=True,
                error=(
                    f"Invalid Copilot agent mode {self.agent_mode!r}; choose one of "
                    f"{', '.join(sorted(VALID_COPILOT_AGENT_MODES))}"
                ),
            )
            return

        self._stopping = False
        event_queue: asyncio.Queue[StreamEvent | None] = asyncio.Queue()
        self._event_queue = event_queue
        state = CopilotEventState()
        effective_session_id = session_id or str(uuid.uuid4())
        terminal_seen = False

        async def permission_handler(request: object, _invocation: dict[str, str]) -> object:
            approved = await self._request_permission(request)
            return PermissionDecisionApproveOnce() if approved else PermissionDecisionReject()

        def on_event(sdk_event: object) -> None:
            nonlocal terminal_seen
            # The runner emits the deterministic session ID itself after
            # create/resume; SDK start events may otherwise duplicate it.
            if _event_type(sdk_event) in {"session.start", "session.resume"}:
                return
            converted = convert_copilot_event(sdk_event, state)
            if converted is not None and self._event_queue is not None:
                terminal_seen = terminal_seen or converted.is_complete
                self._event_queue.put_nowait(converted)

        env = self._build_env()
        connection = None
        # The SDK ships a compatible CLI.  An explicit non-default command opts
        # into a locally managed binary instead.
        if self.command and self.command != "copilot":
            connection = ChildProcessRuntimeConnection(path=self.command, env=env)
        client = CopilotClient(
            connection=connection,
            working_directory=self.working_dir or os.getcwd(),
            env=env,
        )
        self._client = client

        async def execute() -> None:
            nonlocal terminal_seen
            try:
                await client.start()
                # Keep this mapping dynamic so the optional SDK does not become
                # a runtime/type-check dependency for non-Copilot installs.
                session_kwargs: dict[str, Any] = {
                    "on_permission_request": permission_handler,
                    "model": self.model,
                    "reasoning_effort": self.effort,
                    "streaming": True,
                    "working_directory": self.working_dir or os.getcwd(),
                    "on_event": on_event,
                    "enable_config_discovery": True,
                    "enable_skills": True,
                }
                if self.allowed_tools:
                    session_kwargs["available_tools"] = self.allowed_tools
                if self.append_system_prompt:
                    session_kwargs["system_message"] = {
                        "mode": "append",
                        "content": self.append_system_prompt,
                    }

                if session_id:
                    session = await client.resume_session(
                        effective_session_id,
                        **session_kwargs,
                    )
                else:
                    session = await client.create_session(
                        session_id=effective_session_id,
                        **session_kwargs,
                    )
                self._session = session

                # Persist the deterministic ID immediately; the SDK's session
                # start event can be emitted before the callback is registered.
                event_queue.put_nowait(
                    StreamEvent(
                        raw={"type": "session.resume" if session_id else "session.start"},
                        message_type=MessageType.SYSTEM,
                        session_id=effective_session_id,
                    )
                )
                attachments: Any = [
                    {
                        "type": "blob",
                        "data": image.data,
                        "mimeType": image.media_type,
                    }
                    for image in (self.images or [])
                ]
                await session.send_and_wait(
                    prompt,
                    attachments=attachments or None,
                    agent_mode=cast(CopilotAgentMode, self.agent_mode),
                    timeout=float(self.timeout_seconds),
                )
                if not terminal_seen:
                    event_queue.put_nowait(
                        convert_copilot_event(
                            type(
                                "_IdleEvent",
                                (),
                                {
                                    "type": type("_Type", (), {"value": "session.idle"})(),
                                    "data": type("_Data", (), {"aborted": False})(),
                                },
                            )(),
                            state,
                        )
                    )
            except TimeoutError:
                if self._event_queue is not None:
                    self._event_queue.put_nowait(
                        StreamEvent(
                            raw={},
                            message_type=MessageType.RESULT,
                            is_complete=True,
                            error=f"Timed out after {self.timeout_seconds} seconds",
                        )
                    )
            except Exception as exc:
                logger.exception("GitHub Copilot SDK session failed")
                if self._event_queue is not None:
                    self._event_queue.put_nowait(
                        StreamEvent(
                            raw={},
                            message_type=MessageType.RESULT,
                            is_complete=True,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    )
            finally:
                if self._event_queue is not None:
                    self._event_queue.put_nowait(None)

        task = asyncio.create_task(execute(), name=f"copilot-{effective_session_id}")
        try:
            while True:
                event = await self._event_queue.get()
                if event is None:
                    break
                yield event
        finally:
            await task
            await self._cleanup()

    async def _request_permission(self, request: object) -> bool:
        """Bridge an SDK permission callback to ccdb's Discord buttons."""
        if self.dangerously_skip_permissions:
            return True
        if self._event_queue is None:
            return False

        request_id = str(uuid.uuid4())
        tool_input = _object_dict(request)
        kind = str(getattr(request, "kind", tool_input.get("kind", "tool")))
        display_name, _category = _normalise_tool(kind, tool_input)
        full_command_text = getattr(request, "full_command_text", None)
        if kind == "shell" and full_command_text:
            display_name = "Bash"
            tool_input["command"] = full_command_text
        elif kind in {"write", "read"}:
            path = getattr(request, "file_name", None) or getattr(request, "path", None)
            if path:
                tool_input["file_path"] = path

        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[bool] = loop.create_future()
        self._permission_waiters[request_id] = waiter
        self._event_queue.put_nowait(
            StreamEvent(
                raw={"type": "permission.requested"},
                message_type=MessageType.SYSTEM,
                permission_request=PermissionRequest(
                    request_id=request_id,
                    tool_name=display_name,
                    tool_input=tool_input,
                ),
            )
        )
        try:
            return await waiter
        finally:
            self._permission_waiters.pop(request_id, None)

    async def inject_tool_result(self, request_id: str, data: dict) -> None:
        waiter = self._permission_waiters.get(request_id)
        if waiter is not None and not waiter.done():
            waiter.set_result(bool(data.get("approved")))

    async def interrupt(self) -> None:
        self._stopping = True
        if self._session is not None:
            try:
                await self._session.abort()
            except Exception:
                logger.warning("Failed to abort GitHub Copilot session", exc_info=True)

    async def kill(self) -> None:
        await self.interrupt()
        if self._client is not None:
            try:
                await self._client.stop()
            except Exception:
                logger.warning("Failed to stop GitHub Copilot client", exc_info=True)

    async def _cleanup(self) -> None:
        for waiter in self._permission_waiters.values():
            if not waiter.done():
                waiter.set_result(False)
        self._permission_waiters.clear()
        if self._session is not None:
            try:
                await self._session.disconnect()
            except Exception:
                logger.debug("Copilot session disconnect failed", exc_info=True)
        if self._client is not None:
            try:
                await self._client.stop()
            except Exception:
                logger.debug("Copilot client stop failed", exc_info=True)
        self._session = None
        self._client = None

    _STRIPPED_ENV_KEYS = frozenset(
        {
            "CLAUDECODE",
            "DISCORD_BOT_TOKEN",
            "DISCORD_TOKEN",
            "API_SECRET_KEY",
            "CCDB_ZAI_ENV_FILE",
        }
    )

    def _build_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k not in self._STRIPPED_ENV_KEYS}
        if self.api_port is not None:
            env["CCDB_API_URL"] = f"http://127.0.0.1:{self.api_port}"
        if self.api_secret is not None:
            env["CCDB_API_SECRET"] = self.api_secret
        if self.thread_id is not None:
            env["DISCORD_THREAD_ID"] = str(self.thread_id)
        return env

    def describe_api(self) -> str:
        return "GitHub Copilot"

    def describe_sandbox(self) -> str:
        if self.dangerously_skip_permissions:
            return "Copilot permissions bypassed (defers to ccdb's outer boundary)"
        return "GitHub Copilot tool permissions + ccdb outer boundary"
