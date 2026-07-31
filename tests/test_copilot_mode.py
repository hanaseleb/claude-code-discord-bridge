"""Tests for selecting GitHub Copilot's initial agent mode."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import discord
import pytest

from claude_code_core.copilot_runner import CopilotRunner
from claude_discord.backend_factory import BackendFactory
from claude_discord.backend_settings import BackendSettings
from claude_discord.cogs.backend_command import BackendCommandCog
from claude_discord.cogs.claude_chat import ClaudeChatCog
from claude_discord.database.settings_repo import SettingsRepository


async def _settings() -> BackendSettings:
    path = Path(tempfile.mkdtemp()) / "settings.db"
    async with aiosqlite.connect(str(path)) as db:
        await db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        await db.commit()
    return BackendSettings(
        SettingsRepository(str(path)),
        env_backend="copilot",
        env_model_for_claude="sonnet",
        env_model_for_codex="",
    )


def _factory() -> BackendFactory:
    return BackendFactory(
        claude_command="claude",
        codex_command="codex",
        permission_mode="acceptEdits",
        working_dir=None,
        timeout_seconds=300,
        dangerously_skip_permissions=False,
        allowed_tools=None,
        append_system_prompt=None,
        effort=None,
    )


def _chat_cog(settings: BackendSettings) -> ClaudeChatCog:
    cog = ClaudeChatCog.__new__(ClaudeChatCog)
    cog._factory = _factory()  # type: ignore[attr-defined]
    cog._backend_settings = settings  # type: ignore[attr-defined]
    cog.runner = CopilotRunner()  # type: ignore[attr-defined]
    return cog


class TestCopilotModeSettings:
    async def test_defaults_to_interactive(self) -> None:
        settings = await _settings()

        assert await settings.copilot_mode() == "interactive"
        assert await settings.copilot_mode(thread_id=42) == "interactive"

    async def test_thread_mode_overrides_global(self) -> None:
        settings = await _settings()

        await settings.set_copilot_mode("autopilot")
        await settings.set_copilot_mode("interactive", thread_id=42)

        assert await settings.copilot_mode() == "autopilot"
        assert await settings.copilot_mode(thread_id=42) == "interactive"
        assert await settings.copilot_mode(thread_id=7) == "autopilot"

    async def test_rejects_unknown_mode(self) -> None:
        settings = await _settings()

        with pytest.raises(ValueError):
            await settings.set_copilot_mode("reckless")


class TestCopilotModeRunnerResolution:
    async def test_thread_mode_is_applied_to_copilot_runner(self) -> None:
        settings = await _settings()
        await settings.set_copilot_mode("autopilot", thread_id=42)
        cog = _chat_cog(settings)

        runner = await cog._build_runner_for_thread(
            thread_id=42,
            model_override=None,
            tools_override=None,
            fork_session=False,
            working_dir_override=None,
            effort_override=None,
        )

        assert isinstance(runner, CopilotRunner)
        assert runner.agent_mode == "autopilot"


class TestCopilotModeCommand:
    async def test_sets_autopilot_for_current_thread(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = await _settings()
        chat_cog = MagicMock()
        cog = BackendCommandCog(
            MagicMock(),
            settings=settings,
            factory=_factory(),
            chat_cog=chat_cog,
        )
        interaction = MagicMock(spec=discord.Interaction)
        interaction.channel = MagicMock(spec=discord.Thread)
        interaction.channel.id = 42
        interaction.response.send_message = AsyncMock()
        monkeypatch.setattr(
            cog,
            "_thread_id_or_none",
            lambda _interaction: 42,
        )

        await cog.copilot_mode_command.callback(
            cog,
            interaction,
            mode="autopilot",
            scope="thread",
        )

        assert await settings.copilot_mode(thread_id=42) == "autopilot"
        interaction.response.send_message.assert_awaited_once()
