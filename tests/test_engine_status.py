"""Tests for the Codex engine status footer helpers."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claude_discord.discord_ui.engine_status import (
    CodexStatusProvider,
    fetch_codex_rate_limits,
    format_codex_status_line,
)

# Representative `account/rateLimits/read` result (trimmed to what we read).
SAMPLE = {
    "rateLimits": {
        "limitId": "codex",
        "primary": {"usedPercent": 1, "windowDurationMins": 300, "resetsAt": 1782198285},
        "secondary": {"usedPercent": 8, "windowDurationMins": 10080, "resetsAt": 1782361421},
        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
        "planType": "prolite",
        "rateLimitReachedType": None,
    }
}


class TestFormat:
    def test_basic_line(self) -> None:
        line = format_codex_status_line(SAMPLE)
        assert line is not None
        assert "Codex" in line
        assert "5h 1%" in line
        assert "週次 8%" in line
        assert "クレジット 0" in line
        assert "(prolite)" in line

    def test_unlimited_credits(self) -> None:
        data = {"rateLimits": {"primary": {"usedPercent": 5}, "credits": {"unlimited": True}}}
        line = format_codex_status_line(data)
        assert line is not None
        assert "クレジット 無制限" in line

    def test_rounds_fractional_percent(self) -> None:
        data = {"rateLimits": {"primary": {"usedPercent": 12.6}}}
        line = format_codex_status_line(data)
        assert line is not None
        assert "5h 13%" in line

    def test_rate_limit_reached_warning(self) -> None:
        data = {
            "rateLimits": {
                "primary": {"usedPercent": 100},
                "rateLimitReachedType": "rate_limit_reached",
            }
        }
        line = format_codex_status_line(data)
        assert line is not None
        assert "上限到達" in line

    @pytest.mark.parametrize("bad", [None, {}, {"rateLimits": None}, {"rateLimits": {}}, "x"])
    def test_returns_none_for_unusable(self, bad: object) -> None:
        assert format_codex_status_line(bad) is None  # type: ignore[arg-type]


class TestProviderCache:
    async def test_caches_within_ttl(self) -> None:
        calls = {"n": 0}

        async def fake_fetch(_cmd: str) -> dict:
            calls["n"] += 1
            return SAMPLE

        now = {"t": 1000.0}
        prov = CodexStatusProvider("codex", ttl=90.0, fetcher=fake_fetch, clock=lambda: now["t"])

        first = await prov.get_line()
        assert first is not None
        # Second call within TTL → cached, no extra fetch.
        now["t"] = 1050.0
        await prov.get_line()
        assert calls["n"] == 1

        # After TTL expiry → refetch.
        now["t"] = 1200.0
        await prov.get_line()
        assert calls["n"] == 2

    async def test_failure_uses_short_ttl(self) -> None:
        calls = {"n": 0}

        async def failing_fetch(_cmd: str) -> None:
            calls["n"] += 1
            return None

        now = {"t": 0.0}
        prov = CodexStatusProvider(
            "codex", ttl=90.0, fail_ttl=30.0, fetcher=failing_fetch, clock=lambda: now["t"]
        )

        assert await prov.get_line() is None
        # Within fail_ttl → still cached (no refetch).
        now["t"] = 10.0
        assert await prov.get_line() is None
        assert calls["n"] == 1
        # After fail_ttl → refetch.
        now["t"] = 40.0
        assert await prov.get_line() is None
        assert calls["n"] == 2

    async def test_force_bypasses_cache(self) -> None:
        calls = {"n": 0}

        async def fake_fetch(_cmd: str) -> dict:
            calls["n"] += 1
            return SAMPLE

        prov = CodexStatusProvider("codex", fetcher=fake_fetch, clock=lambda: 0.0)
        await prov.get_line()
        await prov.get_line(force=True)
        assert calls["n"] == 2


class TestWorkspaceEnvironment:
    async def test_fetch_injects_codex_home_only_into_child_process(self, monkeypatch) -> None:
        monkeypatch.setenv("CODEX_HOME", "/parent/default")
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "must-not-reach-codex")
        stdin = MagicMock()
        stdin.drain = AsyncMock()
        stdout = MagicMock()
        stdout.readline = AsyncMock(
            return_value=(json.dumps({"jsonrpc": "2.0", "id": 2, "result": SAMPLE}) + "\n").encode()
        )
        process = MagicMock()
        process.stdin = stdin
        process.stdout = stdout
        process.terminate = MagicMock()
        process.wait = AsyncMock(return_value=0)

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as spawn:
            result = await fetch_codex_rate_limits(
                "codex",
                codex_home="/srv/codex/business",
            )

        assert result == SAMPLE
        assert spawn.await_args.kwargs["env"]["CODEX_HOME"] == "/srv/codex/business"
        assert "DISCORD_BOT_TOKEN" not in spawn.await_args.kwargs["env"]
        assert os.environ["CODEX_HOME"] == "/parent/default"
