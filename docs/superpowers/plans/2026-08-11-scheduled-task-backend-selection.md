# Scheduled Task Backend Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a scheduled task in `claude-code-discord-bridge` (ccdb) pin the backend (`claude`/`codex`/`local`/`agui`/`zai`) it runs on, instead of always following whatever backend happens to be active for its thread/global setting when the 30-second master loop fires it.

**Architecture:** Add a nullable `backend` column to `scheduled_tasks`, accept/validate it in the `POST /api/tasks` and `PATCH /api/tasks/{id}` REST contract (the only way tasks are created — there is no Discord slash command for this per Key Design Decision #9), and thread it through `SchedulerCog._run_task` → `build_headless_runner` as a new `backend_override` parameter that takes priority over `BackendSettings.current_backend()`. Model and reasoning-effort resolution are untouched — they still read from `BackendSettings` for whichever backend ends up chosen, so a pinned task keeps following the model/effort configured for that backend.

**Tech Stack:** Python 3.12+, aiosqlite, aiohttp (`ext/api_server.py`), pytest + pytest-asyncio.

## Global Constraints

- **Zero-Config Principle**: omitting `backend` anywhere must reproduce today's exact behavior (task follows the thread/global `/backend` setting at fire time). No existing caller, task, or test may need to change to keep working.
- **`from __future__ import annotations`** in every touched file (already present in all of them).
- **Line length 100 chars max**, ruff-formatted, `ruff check` clean, `pyright` clean (project CI gate).
- **TDD enforced**: write the failing test before the implementation for every code task (see `CLAUDE.md` → Testing).
- **Security audit mandatory** before committing any change to a Cog (`scheduler.py` qualifies) — see `.claude/skills/security-audit/SKILL.md`. The new `backend` value is always checked against the fixed `ALL_BACKENDS` allowlist before it touches anything else, so there is no new injection surface, but the checklist must still be run and its outcome recorded in the commit.
- **No shell=True, no direct string interpolation of user input into subprocess args** — this plan never touches subprocess construction directly; it only chooses which already-validated backend name is handed to the existing `BackendFactory.build()` / `create_backend()` path.

---

## Out of Scope (explicitly, so nobody "fixes" this by accident)

- **Per-task model/effort pinning.** The user asked for backend selection only. `build_headless_runner` keeps resolving model/effort via `BackendSettings.current_model()/current_effort()` for whichever backend is chosen — a pinned task still follows the model/effort currently configured for that backend (global or thread-scoped), it does not freeze the model at creation time. If a future request needs that too, `build_headless_runner` already has the pattern to copy (`backend_override` → add `model_override`/`effort_override` the same way).
- **`webhook_trigger.py` (CI/CD-triggered tasks).** It calls the same `build_headless_runner`, so the new `backend_override` parameter is available to it for free, but wiring a `backend` field into the webhook payload is a separate feature with its own contract (GitHub Actions payload shape) and is not part of this plan.
- **A Discord UI/slash command to create scheduled tasks.** Per `CLAUDE.md` Key Design Decision #9, ccdb deliberately has no such command — a human asks Claude in conversation, and Claude calls the REST API. This plan's job is to make that REST contract expressive enough for Claude to say "pin this to codex," not to add a form.
- **Non-English README translations** (`docs/ko/`, `docs/zh-CN/`, `docs/pt-BR/`, `docs/fr/`, `docs/es/`). Task 4 updates `README.md` (source of truth) and `docs/ja/README.md` (the maintainer's working language) with exact text; the other locales are a follow-up translation pass outside this plan.

---

## File Structure

| File | Responsibility |
|---|---|
| `claude_discord/database/task_repo.py` | Owns the `scheduled_tasks` schema and CRUD. Gets a new nullable `backend` column + `create(..., backend=...)` + `update(..., backend=...)` with a sentinel so "not mentioned" and "explicitly clear" are distinguishable. |
| `claude_discord/ext/api_server.py` | Owns the REST contract. Validates `backend` against `BackendSettings.ALL_BACKENDS` on `POST /api/tasks` and `PATCH /api/tasks/{id}`, returns 400 on an unknown value. `GET /api/tasks` needs no change — it already returns full rows. |
| `claude_discord/cogs/headless_backend.py` | Owns backend resolution for non-chat runs. Gets a new `backend_override: str | None = None` parameter on `build_headless_runner()` that takes priority over `settings.current_backend()` when set. |
| `claude_discord/cogs/scheduler.py` | Owns task execution. `_run_task` passes `task.get("backend")` as `backend_override` into `build_headless_runner`. |
| `tests/test_task_repo.py` | New `TestTaskRepoBackend` class. |
| `tests/test_api_tasks.py` | New `TestTasksBackend` class. |
| `tests/test_scheduler.py` | New override-wins test alongside the existing `test_run_task_uses_current_backend_from_settings` (kept as-is — it is the backward-compatibility regression proof). |
| `README.md`, `docs/ja/README.md`, `docs/backends.md`, `CHANGELOG.md`, `CLAUDE.md` | Doc surface Claude reads to know the contract exists, and that a human reads to understand why it works this way. |

---

### Task 1: `scheduled_tasks.backend` column + repository CRUD

**Files:**
- Modify: `claude_discord/database/task_repo.py:15` (add sentinel), `:35-45` (add migration), `:54-74` (`init_db`), `:157-221` (`create`), `:276-329` (`update`)
- Test: `tests/test_task_repo.py`

**Interfaces:**
- Consumes: nothing new (pure DB layer).
- Produces: `TaskRepository.create(..., backend: str | None = None) -> int`. `TaskRepository.update(..., backend: str | None | object = _NOT_PROVIDED) -> bool` — passing nothing leaves the column untouched, `backend=None` clears it, `backend="codex"` sets it. Every row dict returned by `get`/`get_all`/`get_due` now has a `"backend"` key (`None` when unset). Later tasks rely on exactly this contract.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_task_repo.py` (after `TestTaskRepoUpdate`, before `TestTaskRepoThreadId` — or anywhere at module scope, order doesn't matter to pytest):

```python
class TestTaskRepoBackend:
    """Tests for the backend column — pinning a scheduled task to one backend."""

    async def test_create_without_backend_defaults_to_none(self, repo: TaskRepository) -> None:
        task_id = await repo.create(
            name="no-backend", prompt="p", interval_seconds=60, channel_id=1
        )
        task = await repo.get(task_id)
        assert task is not None
        assert task["backend"] is None

    async def test_create_with_backend(self, repo: TaskRepository) -> None:
        task_id = await repo.create(
            name="pinned",
            prompt="p",
            interval_seconds=60,
            channel_id=1,
            backend="codex",
        )
        task = await repo.get(task_id)
        assert task is not None
        assert task["backend"] == "codex"

    async def test_update_sets_backend(self, repo: TaskRepository) -> None:
        task_id = await repo.create(name="u-backend", prompt="p", interval_seconds=60, channel_id=1)
        result = await repo.update(task_id, backend="zai")
        assert result is True
        task = await repo.get(task_id)
        assert task is not None
        assert task["backend"] == "zai"

    async def test_update_without_backend_leaves_it_unchanged(self, repo: TaskRepository) -> None:
        task_id = await repo.create(
            name="untouched", prompt="p", interval_seconds=60, channel_id=1, backend="codex"
        )
        await repo.update(task_id, prompt="new prompt")
        task = await repo.get(task_id)
        assert task is not None
        assert task["backend"] == "codex"

    async def test_update_clear_backend(self, repo: TaskRepository) -> None:
        task_id = await repo.create(
            name="clear-backend", prompt="p", interval_seconds=60, channel_id=1, backend="codex"
        )
        result = await repo.update(task_id, backend=None)
        assert result is True
        task = await repo.get(task_id)
        assert task is not None
        assert task["backend"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_task_repo.py::TestTaskRepoBackend -v`
Expected: FAIL — `create()` raises `TypeError: create() got an unexpected keyword argument 'backend'` (and, once that's fixed in your head, `task["backend"]` would raise `KeyError` because the column doesn't exist yet).

- [ ] **Step 3: Add the sentinel and migration**

In `claude_discord/database/task_repo.py`, right after `logger = logging.getLogger(__name__)` (line 15), add:

```python
# Sentinel distinguishing "backend not mentioned in this update() call" (leave
# the column untouched) from "backend=None" (explicitly clear the pin, so the
# task resumes following whatever backend is active for its thread/global
# setting). Plain None can't do double duty here the way it does for the
# other optional fields, because None is itself a valid value to persist.
_NOT_PROVIDED = object()
```

Add a new migration block next to `_MIGRATION_ANCHOR` / `_MIGRATION_FOLLOWUP` (after line 45):

```python
# Migration: add backend column (nullable — None means "follow the
# thread/global backend setting", unchanged legacy behavior)
_MIGRATION_BACKEND = """
ALTER TABLE scheduled_tasks ADD COLUMN backend TEXT;
"""
```

- [ ] **Step 4: Wire the migration into `init_db`**

In `init_db` (currently lines 54-74), after the `thread_id`/`one_shot` migration block and before `await db.commit()`, add:

```python
            if "backend" not in columns:
                for stmt in _MIGRATION_BACKEND.strip().split(";"):
                    stmt = stmt.strip()
                    if stmt:
                        await db.execute(stmt)
                logger.info("Migrated scheduled_tasks: added backend")
```

- [ ] **Step 5: Add `backend` to `create()`**

Replace the `create()` method (lines 157-221) with:

```python
    async def create(
        self,
        name: str,
        prompt: str,
        interval_seconds: int,
        channel_id: int,
        *,
        working_dir: str | None = None,
        run_immediately: bool = True,
        anchor_hour: int | None = None,
        anchor_minute: int | None = None,
        thread_id: int | None = None,
        one_shot: bool = False,
        backend: str | None = None,
    ) -> int:
        """Create a new scheduled task. Returns the created ID.

        Args:
            run_immediately: If True (default), set next_run_at = now so the
                task fires on the next master-loop tick. If False, delay by
                interval_seconds (useful for tasks that should wait one full
                cycle before the first run).
            anchor_hour: Optional wall-clock hour (0-23) to snap to.
            anchor_minute: Optional wall-clock minute (0-59) to snap to.
                When anchor_hour is set, next_run_at is calculated as the
                next future occurrence of that time, preventing drift.
            thread_id: Optional Discord thread ID to post into. When set,
                the scheduler posts to this existing thread instead of
                creating a new one (follow-up mode).
            one_shot: If True, the task auto-disables after a single execution.
            backend: Optional backend name (e.g. "claude", "codex", "local",
                "agui", "zai") to pin this task to. When None (default), the
                task follows whichever backend is active for its thread/global
                setting at run time — unchanged legacy behavior.
        """
        now = time.time()
        if anchor_hour is not None and not run_immediately:
            next_run = self._next_anchor(anchor_hour, anchor_minute or 0, interval_seconds)
        elif run_immediately:
            next_run = now
        else:
            next_run = now + interval_seconds
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """INSERT INTO scheduled_tasks
                   (name, prompt, interval_seconds, channel_id, working_dir,
                    enabled, next_run_at, created_at, anchor_hour, anchor_minute,
                    thread_id, one_shot, backend)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    name,
                    prompt,
                    interval_seconds,
                    channel_id,
                    working_dir,
                    next_run,
                    now,
                    anchor_hour,
                    anchor_minute,
                    thread_id,
                    1 if one_shot else 0,
                    backend,
                ),
            )
            await db.commit()
            row_id = cursor.lastrowid
        assert row_id is not None
        logger.info(
            "Scheduled task created: id=%d, name=%s, interval=%ds", row_id, name, interval_seconds
        )
        return row_id
```

- [ ] **Step 6: Add `backend` to `update()`**

Replace the `update()` method (lines 276-329) with:

```python
    async def update(
        self,
        task_id: int,
        *,
        prompt: str | None = None,
        interval_seconds: int | None = None,
        working_dir: str | None = None,
        anchor_hour: int | None = None,
        anchor_minute: int | None = None,
        thread_id: int | None = None,
        backend: str | None | object = _NOT_PROVIDED,
    ) -> bool:
        """Partially update a task. Returns True if updated.

        Set ``anchor_hour=-1`` to clear the anchor (reset to relative mode).
        Set ``thread_id=-1`` to clear the thread (reset to new-thread mode).
        Omit ``backend`` to leave it untouched; pass ``backend=None`` to clear
        the pin (resume following the thread/global setting), or a backend
        name to set/replace it.
        """
        fields: list[str] = []
        values: list[object] = []
        if prompt is not None:
            fields.append("prompt = ?")
            values.append(prompt)
        if interval_seconds is not None:
            fields.append("interval_seconds = ?")
            values.append(interval_seconds)
        if working_dir is not None:
            fields.append("working_dir = ?")
            values.append(working_dir)
        if anchor_hour is not None:
            if anchor_hour < 0:
                # Sentinel: clear anchor
                fields.append("anchor_hour = ?")
                values.append(None)
                fields.append("anchor_minute = ?")
                values.append(None)
            else:
                fields.append("anchor_hour = ?")
                values.append(anchor_hour)
                fields.append("anchor_minute = ?")
                values.append(anchor_minute if anchor_minute is not None else 0)
        if thread_id is not None:
            if thread_id < 0:
                fields.append("thread_id = ?")
                values.append(None)
            else:
                fields.append("thread_id = ?")
                values.append(thread_id)
        if backend is not _NOT_PROVIDED:
            fields.append("backend = ?")
            values.append(backend)
        if not fields:
            return False
        values.append(task_id)
        sql = f"UPDATE scheduled_tasks SET {', '.join(fields)} WHERE id = ?"  # noqa: S608
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(sql, tuple(values))
            await db.commit()
            return cursor.rowcount > 0
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_task_repo.py -v`
Expected: PASS — the full file, not just the new class (guards against breaking `anchor_hour`/`thread_id` clearing logic while editing the same method).

- [ ] **Step 8: Lint, format, type-check**

Run: `uv run ruff check claude_discord/database/task_repo.py && uv run ruff format claude_discord/database/task_repo.py && uv run pyright claude_discord/database/task_repo.py`
Expected: all three clean.

- [ ] **Step 9: Commit**

```bash
git add claude_discord/database/task_repo.py tests/test_task_repo.py
git commit -m "feat: add backend column to scheduled_tasks for per-task backend pinning"
```

---

### Task 2: `POST /api/tasks` and `PATCH /api/tasks/{id}` accept and validate `backend`

**Files:**
- Modify: `claude_discord/ext/api_server.py:38-40` (import), `:614-677` (`create_task`), `:700-763` (`patch_task`)
- Test: `tests/test_api_tasks.py`

**Interfaces:**
- Consumes: `TaskRepository.create(..., backend=...)` / `.update(..., backend=...)` from Task 1. `ALL_BACKENDS` (the tuple `("claude", "codex", "local", "agui", "zai")`) already exported by `claude_discord/backend_settings.py`.
- Produces: `backend` in the `POST`/`PATCH` JSON body — a string in `ALL_BACKENDS` to set/replace, `null`/absent handled per-endpoint (see below) — and in every task object returned by `GET /api/tasks`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_api_tasks.py` (new top-level class, anywhere after the `client` fixture):

```python
class TestTasksBackend:
    """Tests for the optional backend field in /api/tasks."""

    async def test_create_task_with_backend(self, client: TestClient) -> None:
        resp = await client.post(
            "/api/tasks",
            json={
                "name": "pinned-task",
                "prompt": "p",
                "interval_seconds": 60,
                "channel_id": 1,
                "backend": "codex",
            },
        )
        assert resp.status == 201

    async def test_create_task_with_invalid_backend_returns_400(self, client: TestClient) -> None:
        resp = await client.post(
            "/api/tasks",
            json={
                "name": "bad-backend",
                "prompt": "p",
                "interval_seconds": 60,
                "channel_id": 1,
                "backend": "not-a-real-backend",
            },
        )
        assert resp.status == 400

    async def test_created_task_has_backend(
        self, client: TestClient, task_repo: TaskRepository
    ) -> None:
        resp = await client.post(
            "/api/tasks",
            json={
                "name": "with-backend",
                "prompt": "p",
                "interval_seconds": 60,
                "channel_id": 1,
                "backend": "zai",
            },
        )
        task_id = (await resp.json())["id"]
        task = await task_repo.get(task_id)
        assert task is not None
        assert task["backend"] == "zai"

    async def test_task_without_backend_defaults_to_none(
        self, client: TestClient, task_repo: TaskRepository
    ) -> None:
        resp = await client.post(
            "/api/tasks",
            json={"name": "no-backend", "prompt": "p", "interval_seconds": 60, "channel_id": 1},
        )
        task_id = (await resp.json())["id"]
        task = await task_repo.get(task_id)
        assert task is not None
        assert task["backend"] is None

    async def test_patch_sets_backend(
        self, client: TestClient, task_repo: TaskRepository
    ) -> None:
        resp = await client.post(
            "/api/tasks",
            json={"name": "to-pin", "prompt": "p", "interval_seconds": 60, "channel_id": 1},
        )
        task_id = (await resp.json())["id"]
        patch_resp = await client.patch(f"/api/tasks/{task_id}", json={"backend": "codex"})
        assert patch_resp.status == 200
        task = await task_repo.get(task_id)
        assert task is not None
        assert task["backend"] == "codex"

    async def test_patch_with_invalid_backend_returns_400(self, client: TestClient) -> None:
        resp = await client.post(
            "/api/tasks",
            json={"name": "to-pin-bad", "prompt": "p", "interval_seconds": 60, "channel_id": 1},
        )
        task_id = (await resp.json())["id"]
        patch_resp = await client.patch(
            f"/api/tasks/{task_id}", json={"backend": "not-a-real-backend"}
        )
        assert patch_resp.status == 400

    async def test_patch_clears_backend(
        self, client: TestClient, task_repo: TaskRepository
    ) -> None:
        resp = await client.post(
            "/api/tasks",
            json={
                "name": "to-unpin",
                "prompt": "p",
                "interval_seconds": 60,
                "channel_id": 1,
                "backend": "codex",
            },
        )
        task_id = (await resp.json())["id"]
        patch_resp = await client.patch(f"/api/tasks/{task_id}", json={"backend": None})
        assert patch_resp.status == 200
        task = await task_repo.get(task_id)
        assert task is not None
        assert task["backend"] is None

    async def test_list_shows_backend(self, client: TestClient) -> None:
        await client.post(
            "/api/tasks",
            json={
                "name": "listed-with-backend",
                "prompt": "p",
                "interval_seconds": 60,
                "channel_id": 1,
                "backend": "local",
            },
        )
        resp = await client.get("/api/tasks")
        data = await resp.json()
        assert data["tasks"][0]["backend"] == "local"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_api_tasks.py::TestTasksBackend -v`
Expected: FAIL — every "with backend" case gets a 201/200 instead of exercising validation, because `create_task`/`patch_task` currently silently ignore the `backend` key in the JSON body (no error, but `task_repo.create()`/`.update()` are never told about it, so the assertions on `task["backend"]` fail).

- [ ] **Step 3: Import `ALL_BACKENDS`**

In `claude_discord/ext/api_server.py`, the import block currently reads (lines 38-40):

```python
from ..discord_ui.file_sender import send_file_blobs
from ..relay import MODE_INTERRUPT, MODE_QUEUE, VALID_MODES, RelayGuard, build_relay_prompt
from ..session_view import STATE_HISTORY, STATE_RUNNING, build_session_views
```

Add one line before it (alphabetical, so `ruff check` won't re-sort it away from here):

```python
from ..backend_settings import ALL_BACKENDS
from ..discord_ui.file_sender import send_file_blobs
from ..relay import MODE_INTERRUPT, MODE_QUEUE, VALID_MODES, RelayGuard, build_relay_prompt
from ..session_view import STATE_HISTORY, STATE_RUNNING, build_session_views
```

- [ ] **Step 4: Validate and forward `backend` in `create_task`**

In `create_task` (currently lines 614-677), update the docstring and add validation. The method becomes:

```python
    async def create_task(self, request: web.Request) -> web.Response:
        """POST /api/tasks — register a scheduled Claude Code task.

        Body (JSON):
            name: Unique task identifier.
            prompt: Claude Code prompt to run on schedule.
            interval_seconds: How often to run (seconds).
            channel_id: Discord channel ID for thread creation.
            working_dir: (optional) Working directory for Claude.
            run_immediately: (optional, default true) Fire on next loop tick.
            anchor_time: (optional) Wall-clock time ``"HH:MM"`` to snap to,
                preventing time drift. When set, next_run_at is calculated as
                the next future occurrence of that time.
            thread_id: (optional) Discord thread ID for follow-up mode.
                When set, the scheduler posts into this existing thread
                instead of creating a new one.
            one_shot: (optional, default false) If true, auto-disable after
                a single execution.
            backend: (optional) Pin this task to one backend — one of
                ``claude``, ``codex``, ``local``, ``agui``, ``zai``. Omit to
                have the task follow whatever backend is active for its
                thread/global setting when it fires (unchanged behavior).
        """
        if err := self._require_task_repo():
            return err
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        for field in ("name", "prompt", "interval_seconds", "channel_id"):
            if not data.get(field):
                return web.json_response({"error": f"{field} is required"}, status=400)

        try:
            anchor_hour, anchor_minute = self._parse_anchor_time(data.get("anchor_time"))
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        # Parse optional follow-up parameters
        raw_thread_id = data.get("thread_id")
        thread_id: int | None = None
        if raw_thread_id is not None:
            with contextlib.suppress(ValueError, TypeError):
                thread_id = int(raw_thread_id)

        one_shot = bool(data.get("one_shot", False))

        raw_backend = data.get("backend")
        backend: str | None = None
        if raw_backend is not None:
            backend = str(raw_backend)
            if backend not in ALL_BACKENDS:
                return web.json_response(
                    {"error": f"Unknown backend {backend!r}. Choose: {', '.join(ALL_BACKENDS)}."},
                    status=400,
                )

        try:
            task_id = await self.task_repo.create(  # type: ignore[union-attr]
                name=str(data["name"]),
                prompt=str(data["prompt"]),
                interval_seconds=int(data["interval_seconds"]),
                channel_id=int(data["channel_id"]),
                working_dir=data.get("working_dir"),
                run_immediately=bool(data.get("run_immediately", True)),
                anchor_hour=anchor_hour,
                anchor_minute=anchor_minute,
                thread_id=thread_id,
                one_shot=one_shot,
                backend=backend,
            )
        except Exception as exc:
            # Most likely a UNIQUE constraint violation on name
            logger.warning("Failed to create task: %s", exc)
            return web.json_response({"error": "Task name already exists"}, status=409)

        logger.info("Task registered via API: id=%d, name=%s", task_id, _sanitize_log(data["name"]))
        return web.json_response({"status": "created", "id": task_id}, status=201)
```

- [ ] **Step 5: Validate and forward `backend` in `patch_task`**

In `patch_task` (currently lines 700-763), update the docstring and insert a `backend` block right after the existing `anchor_time` block, before `if patch_kwargs:`:

```python
    async def patch_task(self, request: web.Request) -> web.Response:
        """PATCH /api/tasks/{id} — update a task.

        Body (JSON, all optional):
            enabled: bool
            prompt: str
            interval_seconds: int
            working_dir: str
            anchor_time: ``"HH:MM"`` to set, or ``null`` to clear
            backend: backend name (claude/codex/local/agui/zai) to set/replace
                the pin, or ``null`` to clear it and resume following the
                thread/global setting
            next_run_at: float (epoch) — manual schedule reset
        """
        if err := self._require_task_repo():
            return err
        try:
            task_id = int(request.match_info["id"])
        except (ValueError, KeyError):
            return web.json_response({"error": "Invalid ID"}, status=400)

        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        updated = False
        if "enabled" in data:
            result = await self.task_repo.set_enabled(task_id, enabled=bool(data["enabled"]))  # type: ignore[union-attr]
            updated = updated or result

        patch_kwargs: dict[str, object] = {}
        if "prompt" in data:
            patch_kwargs["prompt"] = str(data["prompt"])
        if "interval_seconds" in data:
            patch_kwargs["interval_seconds"] = int(data["interval_seconds"])
        if "working_dir" in data:
            patch_kwargs["working_dir"] = str(data["working_dir"])

        # anchor_time: "HH:MM" to set, null to clear
        if "anchor_time" in data:
            raw_anchor = data["anchor_time"]
            if raw_anchor is None:
                patch_kwargs["anchor_hour"] = -1  # sentinel: clear
            else:
                try:
                    h, m = self._parse_anchor_time(raw_anchor)
                except ValueError as exc:
                    return web.json_response({"error": str(exc)}, status=400)
                patch_kwargs["anchor_hour"] = h
                patch_kwargs["anchor_minute"] = m

        # backend: a backend name to set/replace the pin, null to clear it
        if "backend" in data:
            raw_backend = data["backend"]
            if raw_backend is None:
                patch_kwargs["backend"] = None
            else:
                backend_value = str(raw_backend)
                if backend_value not in ALL_BACKENDS:
                    return web.json_response(
                        {
                            "error": (
                                f"Unknown backend {backend_value!r}. "
                                f"Choose: {', '.join(ALL_BACKENDS)}."
                            )
                        },
                        status=400,
                    )
                patch_kwargs["backend"] = backend_value

        if patch_kwargs:
            result = await self.task_repo.update(task_id, **patch_kwargs)  # type: ignore[union-attr]
            updated = updated or result

        # Manual next_run_at override
        if "next_run_at" in data:
            await self.task_repo._db_execute(  # type: ignore[union-attr]
                "UPDATE scheduled_tasks SET next_run_at = ? WHERE id = ?",
                (float(data["next_run_at"]), task_id),
            )
            updated = True

        if updated:
            return web.json_response({"status": "updated"})
        return web.json_response({"error": "Task not found"}, status=404)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_api_tasks.py -v`
Expected: PASS — full file, to confirm the anchor_time/enabled/working_dir paths in the same two methods still work.

- [ ] **Step 7: Lint, format, type-check**

Run: `uv run ruff check claude_discord/ext/api_server.py && uv run ruff format claude_discord/ext/api_server.py && uv run pyright claude_discord/ext/api_server.py`
Expected: all three clean.

- [ ] **Step 8: Commit**

```bash
git add claude_discord/ext/api_server.py tests/test_api_tasks.py
git commit -m "feat: validate and persist backend field on /api/tasks create and patch"
```

---

### Task 3: `SchedulerCog` honors a task's pinned backend at run time

**Files:**
- Modify: `claude_discord/cogs/headless_backend.py` (whole file — add `backend_override` param and a logger)
- Modify: `claude_discord/cogs/scheduler.py:161-167` (`_run_task`'s `build_headless_runner` call)
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `task["backend"]` (`str | None`) from Task 1's repo rows.
- Produces: `build_headless_runner(..., backend_override: str | None = None) -> SessionBackend` — when `factory`/`settings` are both given and `backend_override` is not `None`, the returned runner is built for that backend (bypassing `settings.current_backend()`); model/effort still resolve from `settings` for that backend. `webhook_trigger.py`'s existing call sites are unaffected (new parameter defaults to `None`, keyword-only).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_scheduler.py`, inside `class TestSchedulerCogMasterLoop` (right after `test_run_task_uses_current_backend_from_settings`, which stays unmodified as the backward-compatibility proof):

```python
    async def test_run_task_backend_override_wins_over_current_setting(
        self, repo: TaskRepository
    ) -> None:
        """A task pinned via `backend` must win over the thread/global setting."""
        import discord

        base_runner = _make_runner()
        zai_runner = MagicMock()
        factory = MagicMock()
        factory.build.return_value = zai_runner
        settings = MagicMock()
        settings.current_backend = AsyncMock(return_value="claude")
        settings.current_model = AsyncMock(return_value=None)
        settings.current_effort = AsyncMock(return_value=None)
        cog = SchedulerCog(
            _make_bot(),
            base_runner,
            repo=repo,
            backend_factory=factory,
            backend_settings=settings,
        )

        task_id = await repo.create(
            name="zai-task", prompt="p", interval_seconds=60, channel_id=99, backend="zai"
        )
        task = await repo.get(task_id)

        mock_thread = AsyncMock(spec=discord.Thread)
        mock_thread.id = 4321
        mock_starter_msg = AsyncMock()
        mock_starter_msg.create_thread = AsyncMock(return_value=mock_thread)
        mock_channel = AsyncMock(spec=discord.TextChannel)
        mock_channel.send = AsyncMock(return_value=mock_starter_msg)
        cog.bot.get_channel = MagicMock(return_value=mock_channel)

        with patch("claude_discord.cogs.scheduler.run_claude_with_config", new_callable=AsyncMock):
            await cog._run_task(task)

        # Pinned to "zai" even though the current setting says "claude".
        factory.build.assert_called_once_with(backend="zai", model=None, thread_id=4321)
        settings.current_model.assert_called_once_with("zai", 4321)
        settings.current_effort.assert_called_once_with("zai", 4321)
        settings.current_backend.assert_not_called()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_scheduler.py::TestSchedulerCogMasterLoop::test_run_task_backend_override_wins_over_current_setting -v`
Expected: FAIL — `factory.build` is called with `backend="claude"` (from `settings.current_backend()`), not `backend="zai"`, because `_run_task` never reads `task["backend"]` yet.

- [ ] **Step 3: Add `backend_override` to `build_headless_runner`**

Replace `claude_discord/cogs/headless_backend.py` in full with:

```python
"""Backend resolution for non-chat automated runs."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from ..claude.runner import _UNSET

if TYPE_CHECKING:
    from claude_code_core.backend import SessionBackend

    from ..backend_factory import BackendFactory
    from ..backend_settings import BackendSettings

logger = logging.getLogger(__name__)


async def build_headless_runner(
    base_runner: SessionBackend,
    *,
    factory: BackendFactory | None = None,
    settings: BackendSettings | None = None,
    thread_id: int | None = None,
    working_dir: str | None = None,
    timeout_seconds: int | None = None,
    allowed_tools: list[str] | None | object = _UNSET,
    permission_mode: str | None = None,
    dangerously_skip_permissions: bool | None = None,
    backend_override: str | None = None,
) -> SessionBackend:
    """Build a runner for scheduler/webhook/custom-cog automation.

    Chat sessions already resolve backend/model in ``ClaudeChatCog``.  Headless
    flows used to clone the startup runner, so a global ``/backend codex`` switch
    did not affect scheduled tasks or failure triage.  When a factory/settings
    pair is available, resolve the current backend at spawn time; otherwise keep
    the legacy clone behaviour.

    ``backend_override`` lets a caller (e.g. a scheduled task pinned to one
    backend) force which backend gets built, bypassing
    ``settings.current_backend()``. Model and effort still resolve from
    ``settings`` for that backend, so a pinned task keeps following whatever
    model/effort is configured for it.
    """
    if factory is not None and settings is not None:
        backend = backend_override if backend_override is not None else await settings.current_backend(
            thread_id
        )
        model = await settings.current_model(backend, thread_id)
        runner = factory.build(backend=backend, model=model, thread_id=thread_id)
        effort = await settings.current_effort(backend, thread_id)
        if effort is not None and hasattr(runner, "effort"):
            runner.effort = effort  # type: ignore[attr-defined]
    else:
        if backend_override is not None:
            logger.warning(
                "backend_override=%r requested but no backend_factory/backend_settings "
                "configured — falling back to the cloned base runner",
                backend_override,
            )
        runner = base_runner.clone(thread_id=thread_id)

    if working_dir is not None:
        runner.working_dir = working_dir
    if timeout_seconds is not None:
        runner.timeout_seconds = timeout_seconds
    if allowed_tools is not _UNSET:
        runner.allowed_tools = allowed_tools  # type: ignore[assignment]
    if permission_mode is not None:
        runner.permission_mode = permission_mode
    if dangerously_skip_permissions is not None:
        runner.dangerously_skip_permissions = dangerously_skip_permissions
    return runner


def backend_factory_from_components(components: Any) -> BackendFactory | None:
    return vars(components).get("backend_factory")


def backend_settings_from_components(components: Any) -> BackendSettings | None:
    return vars(components).get("backend_settings")
```

(If `ruff format` reflows the `backend = backend_override if ... else ...` line differently, accept its formatting — the logic matters, not the line breaks.)

- [ ] **Step 4: Pass the task's pinned backend from `_run_task`**

In `claude_discord/cogs/scheduler.py`, the `build_headless_runner` call (currently lines 161-167) becomes:

```python
            cloned = await build_headless_runner(
                self.runner,
                factory=self.backend_factory,
                settings=self.backend_settings,
                thread_id=surface.thread_key,
                working_dir=task.get("working_dir"),
                backend_override=task.get("backend"),
            )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_scheduler.py -v`
Expected: PASS — full file, including the pre-existing `test_run_task_uses_current_backend_from_settings` (proves the no-override path is unchanged) and `test_run_task_with_thread_id_posts_to_existing_thread` / follow-up tests (proves `backend_override=None` for tasks created without `backend=` doesn't break anything downstream).

- [ ] **Step 6: Lint, format, type-check**

Run: `uv run ruff check claude_discord/cogs/headless_backend.py claude_discord/cogs/scheduler.py && uv run ruff format claude_discord/cogs/headless_backend.py claude_discord/cogs/scheduler.py && uv run pyright claude_discord/cogs/headless_backend.py claude_discord/cogs/scheduler.py`
Expected: all three clean.

- [ ] **Step 7: Run the security audit checklist (Cog change — mandatory per `CLAUDE.md`)**

Open `.claude/skills/security-audit/SKILL.md` and go through the checklist. Record in the commit message (or a PR comment) that: the new `backend` value is validated against the fixed `ALL_BACKENDS` allowlist in `api_server.py` before it ever reaches `task_repo` or `build_headless_runner` — it is never interpolated into a shell string or subprocess argument, only used to select which already-existing `BackendFactory.build(backend=...)` path runs (the exact same path `/backend` already exercises for chat sessions). No new subprocess/env/injection surface is introduced.

- [ ] **Step 8: Commit**

```bash
git add claude_discord/cogs/headless_backend.py claude_discord/cogs/scheduler.py tests/test_scheduler.py
git commit -m "feat: let a scheduled task's pinned backend override the current /backend setting"
```

---

### Task 4: Documentation

**Files:**
- Modify: `README.md:1044-1067` (Scheduled Tasks section), `README.md:1132,1135` (API table)
- Modify: `docs/ja/README.md` (same two spots — Japanese)
- Modify: `docs/backends.md` (insert a subsection after line 46)
- Modify: `CHANGELOG.md` (Unreleased section)
- Modify: `CLAUDE.md` (append one Key Design Decision item)

No automated test — this task is verified by grep + a human read-through in Step 5.

- [ ] **Step 1: Update `README.md`**

Replace the `## Scheduled Tasks` section (lines 1044-1067) with:

```markdown
## Scheduled Tasks

Register periodic Claude Code tasks at runtime — no code changes, no redeploys.

From within a Discord session, Claude can register a task:

```bash
# Claude calls this inside a session:
curl -X POST "$CCDB_API_URL/api/tasks" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Check for outdated deps and open an issue if found", "interval_seconds": 604800}'
```

Or register from your own scripts:

```bash
curl -X POST http://localhost:8080/api/tasks \
  -H "Authorization: Bearer your-secret" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Weekly security scan", "interval_seconds": 604800}'
```

Pin the task to a specific backend by adding `"backend"` (`claude`/`codex`/`local`/`agui`/`zai`).
A pinned task always runs on that backend, regardless of whatever the thread/global `/backend`
setting is when it fires. Omit it and the task keeps following that setting live — the same
behavior as before this field existed:

```bash
curl -X POST "$CCDB_API_URL/api/tasks" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Weekly security scan", "interval_seconds": 604800, "backend": "codex"}'
```

Change or clear the pin later with `PATCH /api/tasks/{id}` (`"backend": "zai"` to set, `"backend": null`
to clear).

The 30-second master loop picks up due tasks and spawns Claude Code sessions automatically.

---
```

Then update the API table rows (lines 1132 and 1135):

```markdown
| POST | `/api/tasks` | Register a scheduled Claude Code task; optionally pin it to a `backend` |
| GET | `/api/tasks` | List registered tasks |
| DELETE | `/api/tasks/{id}` | Remove a task |
| PATCH | `/api/tasks/{id}` | Update a task (enable/disable, change schedule, set/clear `backend`) |
```

- [ ] **Step 2: Update `docs/ja/README.md`**

Replace the curl examples block (currently lines 989-1003) with:

```markdown
```bash
# Claude がセッション内で呼び出す:
curl -X POST "$CCDB_API_URL/api/tasks" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "古い依存関係をチェックして見つかったら Issue を開く", "interval_seconds": 604800}'
```

または独自のスクリプトから登録:

```bash
curl -X POST http://localhost:8080/api/tasks \
  -H "Authorization: Bearer your-secret" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "週次セキュリティスキャン", "interval_seconds": 604800}'
```

`"backend"`（`claude`/`codex`/`local`/`agui`/`zai`）を指定すると、そのタスクを常に指定バックエンドで実行できる。実行時点でのスレッド/グローバルの `/backend` 設定には従わない。省略した場合はこれまでと同じ挙動——実行時点の設定に従う。

```bash
curl -X POST "$CCDB_API_URL/api/tasks" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "週次セキュリティスキャン", "interval_seconds": 604800, "backend": "codex"}'
```

`PATCH /api/tasks/{id}` で後から変更・解除も可能（`"backend": "zai"` で設定、`"backend": null` で解除）。

30 秒マスターループが期限のタスクを検出し、Claude Code セッションを自動起動します。
```

Then update the API table rows (currently lines 1071-1074):

```markdown
| POST | `/api/tasks` | 定期的な Claude Code タスクを登録（任意で `backend` を指定して固定可能） |
| GET | `/api/tasks` | 登録済みタスクの一覧 |
| DELETE | `/api/tasks/{id}` | タスクの削除 |
| PATCH | `/api/tasks/{id}` | タスクの更新（有効/無効、スケジュール変更、`backend` の設定・解除） |
```

- [ ] **Step 3: Add a subsection to `docs/backends.md`**

After line 46 (`its own model and reasoning setting.`) and before `## Claude Code and Codex` (line 48), insert:

```markdown
## Pin a scheduled task to a backend

`/backend` changes the live setting for a conversation or the whole deployment — a scheduled
task that follows it can run on a different backend every time it fires, if someone flips the
switch in between. To fix a task to one backend regardless of that live setting, pass `backend`
when registering it:

```bash
curl -X POST "$CCDB_API_URL/api/tasks" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Weekly dependency audit", "interval_seconds": 604800, "backend": "codex"}'
```

The task's model and reasoning effort still come from whatever `/model` / `/effort` currently has
configured for that backend — pinning only fixes *which* backend runs, not the model on top of it.
Omit `backend` and the task keeps its original behavior: it follows the thread/global `/backend`
setting live, the same as before this field existed.

```

- [ ] **Step 4: Update `CHANGELOG.md`**

Add under `## [Unreleased]` (currently an empty section right after the header):

```markdown
## [Unreleased]

### Added

- **Per-task backend pin for scheduled tasks** — `POST /api/tasks` and `PATCH /api/tasks/{id}`
  accept an optional `backend` field (`claude`/`codex`/`local`/`agui`/`zai`). A pinned task always
  runs on that backend regardless of the thread/global `/backend` setting at fire time; omitting
  it keeps the existing behavior of following whatever backend is currently active.
```

- [ ] **Step 5: Add a Key Design Decision entry to `CLAUDE.md`**

After decision #13 (the local-backend one, ending in "See `docs/local-backend.md`."), add:

```markdown
14. **A pinned task's backend is a stored override, not a live default**: `scheduled_tasks.backend`
    (nullable) lets a task fix which backend it runs on, independent of the thread/global `/backend`
    setting active when it fires. `build_headless_runner`'s `backend_override` parameter takes
    priority over `BackendSettings.current_backend()` when set; model/effort still resolve
    per-backend from `BackendSettings`, so a pinned task keeps following whatever model is
    configured for that backend. Omitting `backend` preserves Decision #8/#9: the task follows the
    thread/global setting live.
```

- [ ] **Step 6: Verify the docs mention the new field consistently**

Run: `grep -rn '"backend"' README.md docs/ja/README.md docs/backends.md CHANGELOG.md CLAUDE.md`
Expected: at least one hit in each of the five files, all describing the same contract (set with a name from `claude`/`codex`/`local`/`agui`/`zai`, clear with `null`).

- [ ] **Step 7: Commit**

```bash
git add README.md docs/ja/README.md docs/backends.md CHANGELOG.md CLAUDE.md
git commit -m "docs: document per-task backend pinning for scheduled tasks"
```

---

### Task 5: Full verification pipeline

**Files:** none (verification only).

- [ ] **Step 1: Full lint + format + type-check + test run**

Run:

```bash
uv run ruff check claude_discord/
uv run ruff format --check claude_discord/
uv run pyright claude_discord/
uv run pytest tests/ -v --cov=claude_discord
```

Expected: all four clean/passing. This is the same gate CI runs (`CLAUDE.md` → Development →
Linting & Formatting / Type Checking, and CI runs both Python 3.12 and 3.13 — running once
locally on whichever interpreter `uv` picked is enough to catch logic errors; CI catches the
cross-version edge cases).

- [ ] **Step 2: Manual smoke test against a running instance (optional but recommended before opening a PR)**

If you have a local ccdb instance running with the REST API enabled (`CCDB_API_PORT` set) and a
real `backend_factory`/`backend_settings` wired (i.e. `/backend` already works in your test
server):

```bash
# 1. Set the live/global backend to something you are NOT about to pin the task to.
#    (Do this in Discord: /backend claude scope:global)

# 2. Register a task pinned to a different backend, firing almost immediately.
curl -X POST "http://localhost:8080/api/tasks" \
  -H "Content-Type: application/json" \
  -d '{"name": "smoke-test", "prompt": "Say hello and name your backend", "interval_seconds": 3600, "channel_id": <a real channel id>, "backend": "codex"}'

# 3. Wait up to 30s for the master loop, then check the new thread: the response should come
#    from Codex, not Claude, proving the pin won over the global "claude" setting.

# 4. Clean up.
curl -X DELETE "http://localhost:8080/api/tasks/<id-from-step-2>"
```

If this environment has no live Discord/API server to test against, skip this step and say so —
the automated test suite (Tasks 1-3) already proves the logic; this step only proves the wiring
in a real deployment.

- [ ] **Step 3: Push the branch and open the PR**

Per `CLAUDE.md` → Git & PR Workflow: branch from `main` (this plan assumes you already did,
e.g. `feature/schedule-task-backend`), squash-mergeable, CI must pass both Python versions.

```bash
git push -u origin <your-branch-name>
gh pr create --title "feat: let scheduled tasks pin a backend" --body "$(cat <<'EOF'
## Summary
- Scheduled tasks can now pin a backend (claude/codex/local/agui/zai) via `POST /api/tasks` /
  `PATCH /api/tasks/{id}`'s new `backend` field, instead of always following the live
  thread/global `/backend` setting.
- Omitting `backend` reproduces the exact previous behavior — fully backward compatible,
  zero-config.

## Test plan
- [x] `uv run pytest tests/ -v --cov=claude_discord`
- [x] `uv run ruff check claude_discord/ && uv run ruff format --check claude_discord/`
- [x] `uv run pyright claude_discord/`
- [x] Security audit checklist run (Cog change) — see Task 3 commit message
EOF
)"
```

---

## Self-Review

**1. Spec coverage.** The user's ask ("スケジュールタスクを設定する際、実行するバックエンドも選べるように") maps to: Task 1 (storage), Task 2 (the only setup surface that exists — REST API), Task 3 (it actually takes effect at run time), Task 4 (Claude/humans can discover the field exists). Nothing in the ask is left unaddressed.

**2. Placeholder scan.** No "TBD"/"handle appropriately"/"similar to Task N" — every step shows the literal diff or full replacement method. The one soft spot is Task 5 Step 2 (manual smoke test), which is explicitly marked optional and has a documented skip condition, not a vague "test manually."

**3. Type consistency.** `backend: str | None` is the type used consistently in `create()` (Task 1), the API layer's local `backend`/`backend_value` variables (Task 2), and `backend_override: str | None` (Task 3) — same shape end to end. The one exception is `update()`'s `backend: str | None | object = _NOT_PROVIDED`, which is deliberately three-valued (not-provided / clear / set) and is documented as such everywhere it's used (Task 1 docstring, Task 2's patch_kwargs usage, this plan's File Structure table).
