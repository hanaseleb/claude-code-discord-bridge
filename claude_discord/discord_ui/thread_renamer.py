"""Thread title auto-renamer for the configured AI backend.

After a new thread is created from a user's first message, this module runs
a lightweight one-shot call to generate a descriptive, short thread title.

The result is applied by renaming the Discord thread via thread.edit(name=...).
Falls back silently (no rename) on any error or timeout.

``suggest_retitle`` is the same call later in a thread's life: it is given the
title the thread already wears and the requests made since, and answers either
``KEEP`` or a replacement.  Asking for a verdict rather than for a title is what
keeps an accurate title stable — a model asked only "title this" always writes
*something*, so the thread would be renamed on every check whether or not its
subject moved.  :mod:`thread_retitle` decides when to ask.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

_PROMPT_TEMPLATE = """\
Output a short thread title (max 80 characters) for the message below.
Rules: single line only, no prefix like "Title:" or "Here's a title:", no quotes, no markdown.

{text}
"""

# The verdict that means "this title is still right". Compared case-insensitively
# against the whole cleaned line, so a title that merely contains the word is
# still a title.
KEEP_VERDICT = "KEEP"

_RETITLE_PROMPT_TEMPLATE = """\
A chat thread is currently titled: {title}

The most recent requests in that thread, oldest first:
{messages}

If the current title still describes what this thread is about, output exactly {keep}.
Only if the subject has clearly moved on, output a better short title (max 80 characters).
Rules: single line only, no prefix like "Title:", no quotes, no markdown, no explanation.
"""

_TIMEOUT_SECONDS = 30
_MAX_TITLE_LENGTH = 90  # Discord thread name limit is 100; leave a small margin

# Prefixes that models sometimes add before the actual title
_PREFIX_RE = re.compile(
    r"^(?:title|タイトル|here'?s?\s+(?:a\s+)?(?:suggested\s+)?title|suggested title)\s*[:：]\s*",
    re.IGNORECASE,
)

# Separator lines: sequences of ─ (U+2500), -, spaces, or backticks
_SEPARATOR_RE = re.compile(r"^[\u2500\-\s`]+$")


def _clean_title(raw: str) -> str:
    """Extract a clean single-line title from raw model output.

    Skips explanatory output mode Insight blocks (★ Insight ... ─────) and
    other structural noise before returning the first meaningful content line.
    """
    in_insight_block = False

    for raw_line in raw.splitlines():
        # Strip backticks used as decorators around insight markers/separators
        stripped = raw_line.strip().strip("`").strip()

        # Detect insight block header (★ Insight marker)
        if "\u2605 Insight" in stripped:  # ★ = U+2605
            in_insight_block = True
            continue

        # Detect insight block end: a separator line of ─ chars after the header
        is_separator = bool(stripped) and bool(_SEPARATOR_RE.fullmatch(stripped))
        if in_insight_block and is_separator:
            in_insight_block = False
            continue

        # Skip lines inside insight blocks and standalone separator lines
        if in_insight_block or is_separator:
            continue

        # Skip empty lines
        if not stripped:
            continue

        # Found the first real content line — apply formatting cleanup
        line = stripped.strip("*_").strip("\"'")
        line = _PREFIX_RE.sub("", line).strip()
        if line:
            return line

    return ""


async def suggest_title(
    user_message: str,
    claude_command: str = "claude",
    env: dict[str, str] | None = None,
    *,
    backend: str = "claude",
    model: str | None = None,
    cwd: str | None = None,
) -> str | None:
    """Call the active backend CLI and return a short thread title.

    Returns None on empty input, timeout, or any error, so the caller can
    keep the original thread name without any visible failure.
    Prompt is passed as a direct argument to the binary (no shell, no injection risk).

    Args:
        claude_command: CLI command path. The legacy parameter name is kept for
            API compatibility; it may point to ``claude`` or ``codex``.
        env: Optional environment dict for the subprocess. When provided
             (e.g. from ``ClaudeRunner._build_env()``), ensures the CLI
             picks up the same API keys and overlay config as main sessions.
             When ``None``, the subprocess inherits the parent environment.
        backend: ``claude`` or ``codex``.
        model: Active backend model, forwarded to Codex's ``--model`` when set.
        cwd: Working directory to pass to the backend subprocess. Codex refuses
             to run outside a git repository unless told otherwise (see
             ``--skip-git-repo-check`` below), and honours ``cwd`` the same way
             the main session runner does.
    """
    if not user_message.strip():
        return None

    prompt = _PROMPT_TEMPLATE.format(text=user_message[:2000])
    return await _ask_for_a_line(
        prompt,
        claude_command=claude_command,
        env=env,
        backend=backend,
        model=model,
        cwd=cwd,
    )


async def suggest_retitle(
    current_title: str,
    recent_messages: Sequence[str],
    claude_command: str = "claude",
    env: dict[str, str] | None = None,
) -> str | None:
    """Return a better title for a thread, or None to keep the current one.

    None is the common — and cheapest — answer: it covers the model's ``KEEP``
    verdict, a suggestion that only restates the current title, empty input, and
    every failure path.  The caller renames nothing when it gets None, so a
    broken or slow CLI leaves the thread exactly as it was.
    """
    messages = [m.strip() for m in recent_messages if m.strip()]
    if not messages or not current_title.strip():
        return None

    prompt = _RETITLE_PROMPT_TEMPLATE.format(
        title=current_title.strip(),
        messages="\n".join(f"- {m[:500]}" for m in messages[-10:]),
        keep=KEEP_VERDICT,
    )
    title = await _ask_for_a_line(prompt, claude_command=claude_command, env=env)
    if title is None:
        return None
    if title.upper() == KEEP_VERDICT:
        return None
    if title.casefold() == current_title.strip().casefold():
        return None
    return title


async def _ask_for_a_line(
    prompt: str,
    claude_command: str,
    env: dict[str, str] | None,
    backend: str = "claude",
    model: str | None = None,
    cwd: str | None = None,
) -> str | None:
    """Run one short one-shot CLI call and return its first meaningful line.

    Prompt is passed as a direct argument to the binary (no shell, no injection
    risk). Returns None on timeout, non-zero exit, empty output or any error.
    """
    # The default ``haiku`` is an Anthropic alias the Z.ai endpoint does not
    # serve, so a title call against a Z.ai-configured env would 404. Detect
    # the endpoint the same way api_provider does and pick a model it serves.
    title_model = "haiku"
    if env and "api.z.ai" in (env.get("ANTHROPIC_BASE_URL") or ""):
        title_model = "glm-4.7"

    if backend == "codex":
        # `codex exec` has no `-p`/one-shot-print flag like the Claude CLI —
        # `exec` already is the one-shot, non-interactive mode. `--sandbox
        # read-only` is enough for a title suggestion (no edits, no shell
        # commands expected) and, unlike the main session runner, does not
        # need to defer to any OS-level sandbox override: a read-only title
        # call has nothing to escalate.
        args = [
            claude_command,
            "exec",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "--ignore-rules",
            "-c",
            "model_reasoning_effort=low",
        ]
        if model:
            args.extend(["--model", model])
        args.append(prompt)
    else:
        args = [
            claude_command,
            "-p",
            "--model",
            title_model,
            # The prompt is a fixed template with the text interpolated into its
            # middle, but the separator is not optional for that reason: it is
            # what makes "could this argument ever start with a dash?" a
            # question nobody has to re-answer when the template changes.
            "--",
            prompt,
        ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
        )
        try:
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT_SECONDS)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            logger.warning("thread title renamer timed out after %ds", _TIMEOUT_SECONDS)
            return None

        if proc.returncode != 0:
            logger.warning("thread title renamer exited with code %d", proc.returncode)
            return None

        raw = stdout.decode(errors="replace")
        stderr_text = _stderr.decode(errors="replace").strip()
        if stderr_text:
            logger.warning("thread title renamer stderr: %s", stderr_text[:500])

        title = _clean_title(raw)

        if not title:
            logger.debug("thread title renamer returned empty output")
            return None

        if len(title) > _MAX_TITLE_LENGTH:
            title = title[:_MAX_TITLE_LENGTH]

        logger.debug("thread title suggestion: %r", title)
        return title

    except Exception:
        logger.warning("thread title renamer failed", exc_info=True)
        return None
