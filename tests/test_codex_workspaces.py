"""Tests for configured Codex workspace parsing."""

from __future__ import annotations

import json

import pytest

from claude_discord.codex_workspaces import parse_codex_workspaces


def test_parse_codex_workspaces_normalizes_absolute_paths(tmp_path) -> None:
    personal = tmp_path / "personal"
    business = tmp_path / "business"
    raw = json.dumps({"personal": str(personal), "business": str(business)})

    assert parse_codex_workspaces(raw) == {
        "personal": str(personal.resolve()),
        "business": str(business.resolve()),
    }


def test_parse_codex_workspaces_empty_value_disables_feature() -> None:
    assert parse_codex_workspaces("") == {}


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("[]", "JSON object"),
        ('{"bad name": "/tmp/a"}', "workspace name"),
        ('{"ok": "relative/path"}', "absolute"),
        ('{"ok": 123}', "path must be a string"),
    ],
)
def test_parse_codex_workspaces_rejects_invalid_configuration(raw: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_codex_workspaces(raw)


def test_parse_codex_workspaces_rejects_duplicate_homes(tmp_path) -> None:
    home = str((tmp_path / "shared").resolve())

    with pytest.raises(ValueError, match="same CODEX_HOME"):
        parse_codex_workspaces(json.dumps({"one": home, "two": home}))
