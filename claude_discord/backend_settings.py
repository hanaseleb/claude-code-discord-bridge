"""Persistent, runtime-mutable backend/model selection.

Reads and writes the current backend (claude/codex) and per-backend
model preference to ``SettingsRepository`` (sqlite key-value store).

Resolution order for any field:
    1. Thread-scoped override (when ``thread_id`` is given)
    2. Global setting
    3. Environment default (passed to constructor)
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .database.settings_repo import SettingsRepository

logger = logging.getLogger(__name__)

# Valid backend names. Keep in sync with claude_code_core.backend.create_backend().
ALL_BACKENDS = ("claude", "codex")

# Settings keys
BACKEND_GLOBAL = "backend.global"
BACKEND_THREAD_PREFIX = "backend.thread."  # + thread_id
MODEL_GLOBAL_PREFIX = "model.global."  # + backend
MODEL_THREAD_PREFIX = "model.thread."  # + thread_id + "." + backend
EFFORT_GLOBAL_PREFIX = "effort.global."  # + backend
EFFORT_THREAD_PREFIX = "effort.thread."  # + thread_id + "." + backend
CODEX_WORKSPACE_GLOBAL = "workspace.codex.global"
CODEX_WORKSPACE_THREAD_PREFIX = "workspace.codex.thread."  # + thread_id

# Codex status footer toggle (2-layer: global default + per-thread override).
#   "auto" — show the Codex status line only when it can actually be fetched
#            (codex installed + logged in). Invisible for Claude-only users.
#   "on"   — always attempt; surface a hint when the fetch fails.
#   "off"  — never show the Codex status line.
CODEX_STATUS_GLOBAL = "status.codex.global"
CODEX_STATUS_THREAD_PREFIX = "status.codex.thread."  # + thread_id
CODEX_STATUS_MODES = ("auto", "on", "off")
CODEX_STATUS_DEFAULT = "auto"


def session_is_resumable(
    stored_backend: str | None,
    current_backend: str,
    stored_codex_workspace: str | None = None,
    current_codex_workspace: str | None = None,
) -> bool:
    """Can ``current_backend`` resume a session ID minted by ``stored_backend``?

    Claude and Codex keep separate session stores, so handing a Codex rollout ID
    to ``claude --resume`` (or vice versa) fails at the CLI level. When the
    stored backend is unknown (records written before the ``backend`` column
    existed) we assume it is compatible — the old behaviour.
    """
    if not stored_backend:
        return True
    if stored_backend != current_backend:
        return False
    if current_backend == "codex" and current_codex_workspace is not None:
        return stored_codex_workspace == current_codex_workspace
    return True


class BackendSettings:
    """Thin wrapper around SettingsRepository that resolves backend/model."""

    def __init__(
        self,
        repo: SettingsRepository,
        *,
        env_backend: str,
        env_model_for_claude: str,
        env_model_for_codex: str,
        codex_workspaces: Mapping[str, str] | None = None,
        env_codex_workspace: str | None = None,
    ) -> None:
        self.repo = repo
        self._env_backend = env_backend if env_backend in ALL_BACKENDS else "claude"
        self._env_model = {
            "claude": env_model_for_claude or "",
            "codex": env_model_for_codex or "",
        }
        self._codex_workspaces = dict(codex_workspaces or {})
        if env_codex_workspace and env_codex_workspace not in self._codex_workspaces:
            raise ValueError(
                f"CCDB_CODEX_WORKSPACE {env_codex_workspace!r} is not present "
                "in CCDB_CODEX_WORKSPACES"
            )
        if env_codex_workspace:
            self._env_codex_workspace = env_codex_workspace
        else:
            self._env_codex_workspace = next(iter(self._codex_workspaces), None)

    @property
    def available_codex_workspaces(self) -> tuple[str, ...]:
        """Configured workspace names in administrator-defined order."""
        return tuple(self._codex_workspaces)

    def codex_home(self, workspace: str | None) -> str | None:
        """Return the configured CODEX_HOME for a workspace name."""
        if workspace is None:
            return None
        return self._codex_workspaces.get(workspace)

    # ── Resolution ──────────────────────────────────────────

    async def current_backend(self, thread_id: int | None = None) -> str:
        """Return the active backend for the given thread (or globally)."""
        if thread_id is not None:
            v = await self.repo.get(f"{BACKEND_THREAD_PREFIX}{thread_id}")
            if v in ALL_BACKENDS:
                return v
        v = await self.repo.get(BACKEND_GLOBAL)
        if v in ALL_BACKENDS:
            return v
        return self._env_backend

    async def explicit_model(self, backend: str, thread_id: int | None = None) -> str | None:
        """Return only the EXPLICITLY-stored model — env fallback is NOT consulted.

        Use this when callers want to distinguish 'user explicitly set a
        per-backend model via /model' from 'we fell back to whatever was
        in .env'. ``current_model()`` mixes those two together; this
        method keeps them apart.

        Resolution: thread > global > None.
        """
        if backend not in ALL_BACKENDS:
            return None
        if thread_id is not None:
            v = await self.repo.get(f"{MODEL_THREAD_PREFIX}{thread_id}.{backend}")
            if v:
                return v
        v = await self.repo.get(f"{MODEL_GLOBAL_PREFIX}{backend}")
        return v if v else None

    async def current_model(self, backend: str, thread_id: int | None = None) -> str | None:
        """Return the model for the given backend, or None if no override.

        When ``None`` is returned the caller should fall back to the
        backend factorys built-in default (e.g. "sonnet" / "gpt-5.4").
        """
        if backend not in ALL_BACKENDS:
            return None
        if thread_id is not None:
            v = await self.repo.get(f"{MODEL_THREAD_PREFIX}{thread_id}.{backend}")
            if v:
                return v
        v = await self.repo.get(f"{MODEL_GLOBAL_PREFIX}{backend}")
        if v:
            return v
        return self._env_model.get(backend) or None

    async def current_effort(self, backend: str, thread_id: int | None = None) -> str | None:
        """Return the reasoning-effort override for ``backend``, or None.

        ``None`` means "no override stored" — the caller should let the
        backend CLI use its own default (e.g. Codex's ``model_reasoning_effort``
        in config.toml). Resolution: thread > global > None.
        """
        if backend not in ALL_BACKENDS:
            return None
        if thread_id is not None:
            v = await self.repo.get(f"{EFFORT_THREAD_PREFIX}{thread_id}.{backend}")
            if v:
                return v
        v = await self.repo.get(f"{EFFORT_GLOBAL_PREFIX}{backend}")
        return v if v else None

    async def current_codex_workspace(self, thread_id: int | None = None) -> str | None:
        """Return the named Codex workspace for this thread, or None if disabled."""
        if not self._codex_workspaces:
            return None
        if thread_id is not None:
            value = await self.repo.get(f"{CODEX_WORKSPACE_THREAD_PREFIX}{thread_id}")
            if value in self._codex_workspaces:
                return value
        value = await self.repo.get(CODEX_WORKSPACE_GLOBAL)
        if value in self._codex_workspaces:
            return value
        return self._env_codex_workspace

    async def codex_status_mode(self, thread_id: int | None = None) -> str:
        """Return the Codex status footer mode for this thread (or globally).

        Resolution: thread override > global > ``CODEX_STATUS_DEFAULT`` (auto).
        """
        if thread_id is not None:
            v = await self.repo.get(f"{CODEX_STATUS_THREAD_PREFIX}{thread_id}")
            if v in CODEX_STATUS_MODES:
                return v
        v = await self.repo.get(CODEX_STATUS_GLOBAL)
        if v in CODEX_STATUS_MODES:
            return v
        return CODEX_STATUS_DEFAULT

    # ── Mutation ────────────────────────────────────────────

    async def set_codex_status_mode(self, mode: str, *, thread_id: int | None = None) -> None:
        if mode not in CODEX_STATUS_MODES:
            raise ValueError(f"unknown codex status mode {mode!r}")
        if thread_id is not None:
            await self.repo.set(f"{CODEX_STATUS_THREAD_PREFIX}{thread_id}", mode)
            logger.info("codex status set: thread=%d -> %s", thread_id, mode)
        else:
            await self.repo.set(CODEX_STATUS_GLOBAL, mode)
            logger.info("codex status set: global -> %s", mode)

    async def set_codex_workspace(self, workspace: str, *, thread_id: int | None = None) -> None:
        """Persist a preconfigured workspace name without accepting paths."""
        if workspace not in self._codex_workspaces:
            raise ValueError(f"unknown Codex workspace {workspace!r}")
        if thread_id is not None:
            await self.repo.set(f"{CODEX_WORKSPACE_THREAD_PREFIX}{thread_id}", workspace)
            logger.info("Codex workspace set: thread=%d -> %s", thread_id, workspace)
        else:
            await self.repo.set(CODEX_WORKSPACE_GLOBAL, workspace)
            logger.info("Codex workspace set: global -> %s", workspace)

    async def set_backend(self, backend: str, *, thread_id: int | None = None) -> None:
        if backend not in ALL_BACKENDS:
            raise ValueError(f"unknown backend {backend!r}")
        if thread_id is not None:
            await self.repo.set(f"{BACKEND_THREAD_PREFIX}{thread_id}", backend)
            logger.info("backend set: thread=%d -> %s", thread_id, backend)
        else:
            await self.repo.set(BACKEND_GLOBAL, backend)
            logger.info("backend set: global -> %s", backend)

    async def set_model(self, backend: str, model: str, *, thread_id: int | None = None) -> None:
        if backend not in ALL_BACKENDS:
            raise ValueError(f"unknown backend {backend!r}")
        if not model:
            raise ValueError("model must not be empty")
        if thread_id is not None:
            await self.repo.set(f"{MODEL_THREAD_PREFIX}{thread_id}.{backend}", model)
            logger.info("model set: thread=%d backend=%s -> %s", thread_id, backend, model)
        else:
            await self.repo.set(f"{MODEL_GLOBAL_PREFIX}{backend}", model)
            logger.info("model set: global backend=%s -> %s", backend, model)

    async def set_effort(self, backend: str, effort: str, *, thread_id: int | None = None) -> None:
        if backend not in ALL_BACKENDS:
            raise ValueError(f"unknown backend {backend!r}")
        if not effort:
            raise ValueError("effort must not be empty")
        if thread_id is not None:
            await self.repo.set(f"{EFFORT_THREAD_PREFIX}{thread_id}.{backend}", effort)
            logger.info("effort set: thread=%d backend=%s -> %s", thread_id, backend, effort)
        else:
            await self.repo.set(f"{EFFORT_GLOBAL_PREFIX}{backend}", effort)
            logger.info("effort set: global backend=%s -> %s", backend, effort)

    async def clear_effort(self, backend: str, *, thread_id: int | None = None) -> bool:
        """Remove a stored effort override. Returns True if something was deleted."""
        if backend not in ALL_BACKENDS:
            raise ValueError(f"unknown backend {backend!r}")
        if thread_id is not None:
            return await self.repo.delete(f"{EFFORT_THREAD_PREFIX}{thread_id}.{backend}")
        return await self.repo.delete(f"{EFFORT_GLOBAL_PREFIX}{backend}")

    async def clear_thread_overrides(self, thread_id: int) -> int:
        """Remove all thread-scoped overrides. Returns count deleted."""
        deleted = 0
        if await self.repo.delete(f"{BACKEND_THREAD_PREFIX}{thread_id}"):
            deleted += 1
        for b in ALL_BACKENDS:
            if await self.repo.delete(f"{MODEL_THREAD_PREFIX}{thread_id}.{b}"):
                deleted += 1
            if await self.repo.delete(f"{EFFORT_THREAD_PREFIX}{thread_id}.{b}"):
                deleted += 1
        if await self.repo.delete(f"{CODEX_STATUS_THREAD_PREFIX}{thread_id}"):
            deleted += 1
        if await self.repo.delete(f"{CODEX_WORKSPACE_THREAD_PREFIX}{thread_id}"):
            deleted += 1
        return deleted
