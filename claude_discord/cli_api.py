"""ccdb api — thin CLI wrapper over the REST control plane.

Replaces the raw ``curl $CCDB_API_URL/...`` pattern Claude Code sessions use
for inter-session coordination (lounge posts, claims, thread relay, session
list). ``CCDB_API_URL`` / ``CCDB_API_SECRET`` / ``DISCORD_THREAD_ID`` are read
from the environment, and the same HTTP calls the curl one-liners made are
issued here — so a single ``Bash(ccdb:*)`` permission rule subsumes every
endpoint and the agent's auto-mode classifier no longer mistakes coordination
traffic for an arbitrary external-system write.

This is a convenience wrapper, not a new contract: every endpoint already
exists on the ccdb REST API (``ext/api_server.py``) and the curl form keeps
working. The wrapper just makes the agent-driven path ergonomic and
permission-friendly — especially for Z.ai (GLM) sessions whose classifier is
quick to flag ad-hoc ``curl`` POSTs.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode


class ApiConfigError(Exception):
    """Raised when a required environment variable is missing."""


def _base_url() -> str:
    """Resolve and normalise ``CCDB_API_URL``."""
    url = os.environ.get("CCDB_API_URL")
    if not url:
        raise ApiConfigError(
            "CCDB_API_URL is not set. Run inside a ccdb session, "
            "or export CCDB_API_URL=<http://host:port>."
        )
    return url.rstrip("/")


def _thread_id(default: str | None = None) -> str | None:
    """Return the explicit value, else ``DISCORD_THREAD_ID``, else default."""
    return os.environ.get("DISCORD_THREAD_ID", default)


def _headers(*, json_body: bool) -> dict[str, str]:
    """Build request headers, attaching the Bearer secret when present."""
    headers: dict[str, str] = {}
    if json_body:
        headers["Content-Type"] = "application/json"
    secret = os.environ.get("CCDB_API_SECRET")
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    return headers


def _drop_none(mapping: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in mapping.items() if v is not None}


def api_request(
    method: str,
    path: str,
    *,
    body: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
) -> Any:
    """Issue a single HTTP call to the ccdb REST API and return decoded JSON.

    Exits the process with a clear message on transport or HTTP errors so the
    CLI subcommands can stay focused on argument wiring.
    """
    url = _base_url() + path
    if query:
        qs = urlencode(_drop_none(dict(query)))
        if qs:
            url = f"{url}?{qs}"

    data: bytes | None = None
    headers = _headers(json_body=body is not None)
    if body is not None:
        data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"HTTP {exc.code} {exc.reason}\n{detail}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"Could not reach ccdb API at {url}: {exc.reason}", file=sys.stderr)
        sys.exit(2)

    if status == 204 or not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _emit(result: Any) -> int:
    """Print a command result as JSON (or nothing for empty bodies)."""
    if result is None:
        return 0
    if isinstance(result, str):
        print(result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------
# Command handlers — each wires argparse args to one api_request call.
# ---------------------------------------------------------------------------


def cmd_lounge_post(args: Any) -> int:
    thread_id = args.thread_id if args.thread_id is not None else _thread_id()
    body = _drop_none({"message": args.message, "label": args.label, "thread_id": thread_id})
    return _emit(api_request("POST", "/api/lounge", body=body))


def cmd_message_send(args: Any) -> int:
    from_thread = args.from_thread
    if from_thread is None and args.from_env:
        from_thread = _thread_id()
    body = _drop_none(
        {
            "text": args.text,
            "from_thread": from_thread,
            "mode": args.mode,
            "hop": args.hop,
        }
    )
    return _emit(api_request("POST", f"/api/threads/{args.thread}/message", body=body))


def cmd_claim_set(args: Any) -> int:
    thread_id = args.thread_id if args.thread_id is not None else _thread_id()
    body = _drop_none({"resource": args.resource, "thread_id": thread_id, "note": args.note})
    return _emit(api_request("POST", "/api/claims", body=body))


def cmd_claim_release(args: Any) -> int:
    thread_id = args.thread_id if args.thread_id is not None else _thread_id()
    query = _drop_none({"resource": args.resource, "thread_id": thread_id})
    return _emit(api_request("DELETE", "/api/claims", query=query))


def cmd_claim_list(args: Any) -> int:
    return _emit(api_request("GET", "/api/claims"))


def cmd_sessions(args: Any) -> int:
    exclude = args.exclude_thread
    if exclude is None and args.exclude_self:
        exclude = _thread_id()
    query = _drop_none({"exclude_thread": exclude})
    return _emit(api_request("GET", "/api/sessions", query=query or None))


def cmd_thread_messages(args: Any) -> int:
    query = _drop_none({"limit": args.limit})
    return _emit(api_request("GET", f"/api/threads/{args.thread}/messages", query=query or None))


# ---------------------------------------------------------------------------
# argparse wiring — nested subcommand groups.
# ---------------------------------------------------------------------------

_HELP = """\
Call the ccdb REST API without raw curl.

Reads CCDB_API_URL / CCDB_API_SECRET / DISCORD_THREAD_ID from the environment
and issues the same HTTP calls the curl one-liners did. Designed for agent
sessions so a single `Bash(ccdb:*)` permission rule covers every endpoint.

Subcommands:
  lounge post      Post a message to the AI Lounge
  message send     Relay a message to another thread's session
  claim set        Claim a resource before working on it
  claim release    Release a held claim
  claim list       List active claims
  sessions         List live sessions
  thread messages  Read recent messages from a thread
"""


def add_api_subparser(parser: Any) -> None:
    """Register the ``ccdb api`` subcommand tree on the given parser."""
    api = parser.add_parser("api", help="Call the ccdb REST API (replaces raw curl)")
    api_sub = api.add_subparsers(dest="api_command", metavar="API_COMMAND")

    # lounge post
    lounge = api_sub.add_parser("lounge", help="AI Lounge")
    lounge_sub = lounge.add_subparsers(dest="lounge_command", metavar="LOUNGE_COMMAND")
    lounge_post = lounge_sub.add_parser("post", help="Post a message")
    lounge_post.add_argument("-m", "--message", required=True)
    lounge_post.add_argument("--label", default=None)
    lounge_post.add_argument("--thread-id", default=None)
    lounge_post.set_defaults(_handler=cmd_lounge_post)

    # message send
    message = api_sub.add_parser("message", help="Relay a message to another thread")
    message_sub = message.add_subparsers(dest="message_command", metavar="MESSAGE_COMMAND")
    message_send = message_sub.add_parser("send", help="Send a relayed message")
    message_send.add_argument("--thread", required=True, help="Destination thread id")
    message_send.add_argument("-t", "--text", required=True)
    message_send.add_argument(
        "--from-thread", default=None, help="Sender thread id (default: --from-env)"
    )
    message_send.add_argument(
        "--from-env",
        action="store_true",
        help="Use $DISCORD_THREAD_ID as the sender thread id",
    )
    message_send.add_argument("--mode", default="queue", choices=["queue", "interrupt"])
    message_send.add_argument("--hop", type=int, default=0)
    message_send.set_defaults(_handler=cmd_message_send)

    # claim set / release / list
    claim = api_sub.add_parser("claim", help="Resource claims")
    claim_sub = claim.add_subparsers(dest="claim_command", metavar="CLAIM_COMMAND")
    claim_set = claim_sub.add_parser("set", help="Acquire a claim")
    claim_set.add_argument("--resource", required=True)
    claim_set.add_argument("--note", default=None)
    claim_set.add_argument("--thread-id", default=None)
    claim_set.set_defaults(_handler=cmd_claim_set)
    claim_release = claim_sub.add_parser("release", help="Release a claim")
    claim_release.add_argument("--resource", required=True)
    claim_release.add_argument("--thread-id", default=None)
    claim_release.set_defaults(_handler=cmd_claim_release)
    claim_list = claim_sub.add_parser("list", help="List active claims")
    claim_list.set_defaults(_handler=cmd_claim_list)

    # sessions
    sessions = api_sub.add_parser("sessions", help="List live sessions")
    sessions.add_argument("--exclude-thread", default=None)
    sessions.add_argument(
        "--exclude-self",
        action="store_true",
        help="Exclude $DISCORD_THREAD_ID from the list",
    )
    sessions.set_defaults(_handler=cmd_sessions)

    # thread messages
    thread = api_sub.add_parser("thread", help="Inspect a thread")
    thread_sub = thread.add_subparsers(dest="thread_command", metavar="THREAD_COMMAND")
    thread_msgs = thread_sub.add_parser("messages", help="Read recent messages")
    thread_msgs.add_argument("--thread", required=True)
    thread_msgs.add_argument("--limit", type=int, default=30)
    thread_msgs.set_defaults(_handler=cmd_thread_messages)

    api.set_defaults(_help=_HELP)


def dispatch(args: Any) -> int:
    """Dispatch a parsed ``ccdb api ...`` invocation. Returns an exit code."""
    handler = getattr(args, "_handler", None)
    if handler is None:
        sys.stdout.write(getattr(args, "_help", _HELP))
        return 0
    return handler(args)
