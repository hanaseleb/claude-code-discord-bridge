"""The /backend confirmation surfaces a per-backend icon.

Each backend has a concrete emoji (Claude 🤖, Codex 🌀, Z.ai 🐉, …) that
prefixes the ``Backend set to …`` confirmation. These tests pin the icon so
a backend does not silently fall back to another's emoji.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import discord

from claude_discord.backend_factory import BackendFactory
from claude_discord.backend_settings import BackendSettings
from claude_discord.cogs.backend_command import BackendCommandCog
from claude_discord.database.settings_repo import SettingsRepository


async def _new_settings_repo() -> SettingsRepository:
    tmp = Path(tempfile.mkdtemp()) / "settings.db"
    async with aiosqlite.connect(str(tmp)) as db:
        await db.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        await db.commit()
    return SettingsRepository(str(tmp))


def _make_cog(settings: BackendSettings) -> BackendCommandCog:
    bot = MagicMock()
    factory = BackendFactory(
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
    chat_cog = MagicMock()
    chat_cog.runner = MagicMock()
    chat_cog.runner.model = "sonnet"
    chat_cog.repo = MagicMock()
    chat_cog.repo.delete = AsyncMock(return_value=True)
    return BackendCommandCog(bot, settings=settings, factory=factory, chat_cog=chat_cog)


def _make_thread_interaction(thread_id: int) -> MagicMock:
    interaction = MagicMock()
    thread = MagicMock(spec=discord.Thread)
    thread.id = thread_id
    interaction.channel = thread
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


def _sent_content(interaction: MagicMock) -> str:
    call = interaction.response.send_message.call_args
    assert call is not None
    return call.kwargs.get("content") or call.args[0]


class TestBackendConfirmationIcon:
    async def test_zai_confirmation_carries_dragon(self) -> None:
        repo = await _new_settings_repo()
        settings = BackendSettings(
            repo,
            env_backend="claude",
            env_model_for_claude="sonnet",
            env_model_for_codex="",
        )
        cog = _make_cog(settings)
        interaction = _make_thread_interaction(thread_id=42)

        await cog.backend_command.callback(cog, interaction, name="zai", scope="thread")

        message = _sent_content(interaction)
        assert "\U0001f409" in message  # 🐉 dragon
        assert "\U0001f7e3" not in message  # not the old purple circle
        assert "\U0001f916" not in message  # not Claude's robot

    async def test_claude_confirmation_carries_robot(self) -> None:
        repo = await _new_settings_repo()
        settings = BackendSettings(
            repo,
            env_backend="claude",
            env_model_for_claude="sonnet",
            env_model_for_codex="",
        )
        await settings.set_backend("claude", thread_id=42)
        cog = _make_cog(settings)
        interaction = _make_thread_interaction(thread_id=42)

        await cog.backend_command.callback(cog, interaction, name="claude", scope="thread")

        message = _sent_content(interaction)
        assert "\U0001f916" in message  # 🤖 robot
