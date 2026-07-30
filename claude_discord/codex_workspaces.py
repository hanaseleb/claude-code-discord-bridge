"""Configuration parsing for named Codex authentication workspaces."""

from __future__ import annotations

import json
import re
from pathlib import Path

_WORKSPACE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
MAX_CODEX_WORKSPACES = 25


def parse_codex_workspaces(raw: str) -> dict[str, str]:
    """Parse ``CCDB_CODEX_WORKSPACES`` into validated name-to-home mappings.

    The Discord command only accepts names from this mapping. Paths therefore
    remain administrator-controlled and never come from interaction input.
    """
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("CCDB_CODEX_WORKSPACES must be valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("CCDB_CODEX_WORKSPACES must be a JSON object")
    if len(value) > MAX_CODEX_WORKSPACES:
        raise ValueError(f"CCDB_CODEX_WORKSPACES supports at most {MAX_CODEX_WORKSPACES} entries")

    result: dict[str, str] = {}
    homes: set[str] = set()
    for name, configured_path in value.items():
        if not isinstance(name, str) or not _WORKSPACE_NAME.fullmatch(name):
            raise ValueError(
                "each Codex workspace name must use 1-32 ASCII letters, numbers, _ or -"
            )
        if not isinstance(configured_path, str):
            raise ValueError(f"Codex workspace {name!r} path must be a string")
        path = Path(configured_path).expanduser()
        if not path.is_absolute():
            raise ValueError(f"Codex workspace {name!r} path must be absolute")
        normalized = str(path.resolve(strict=False))
        if normalized in homes:
            raise ValueError("two Codex workspaces cannot use the same CODEX_HOME")
        homes.add(normalized)
        result[name] = normalized
    return result
