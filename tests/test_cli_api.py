"""Tests for the ``ccdb api`` CLI wrapper (``claude_discord.cli_api``).

Covers:
  - environment resolution (CCDB_API_URL / CCDB_API_SECRET / DISCORD_THREAD_ID)
  - the shared ``api_request`` helper (correct URL, method, headers, body)
  - each command handler wiring its args into the right HTTP call
"""

from __future__ import annotations

import io
import json
import urllib.error
from types import SimpleNamespace

import pytest

from claude_discord import cli_api

# ---------------------------------------------------------------------------
# environment resolution
# ---------------------------------------------------------------------------


def test_base_url_requires_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CCDB_API_URL", raising=False)
    with pytest.raises(cli_api.ApiConfigError):
        cli_api._base_url()


def test_base_url_strips_trailing_slash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CCDB_API_URL", "http://localhost:8081/")
    assert cli_api._base_url() == "http://localhost:8081"


def test_thread_id_falls_back_to_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_THREAD_ID", "111")
    assert cli_api._thread_id() == "111"
    assert cli_api._thread_id("explicit") == "111"


def test_thread_id_explicit_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DISCORD_THREAD_ID", raising=False)
    assert cli_api._thread_id() is None
    assert cli_api._thread_id("explicit") == "explicit"


def test_headers_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CCDB_API_SECRET", raising=False)
    headers = cli_api._headers(json_body=True)
    assert headers["Content-Type"] == "application/json"
    assert "Authorization" not in headers


def test_headers_with_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CCDB_API_SECRET", "s3cret")
    headers = cli_api._headers(json_body=False)
    assert headers["Authorization"] == "Bearer s3cret"
    assert "Content-Type" not in headers


# ---------------------------------------------------------------------------
# api_request — urlopen is mocked so we assert the wire shape.
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def test_api_request_get_with_query(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CCDB_API_URL", "http://h:1")
    captured: dict[str, object] = {}

    def fake_urlopen(req: urllib.request.Request) -> _FakeResponse:
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = dict(req.header_items())
        return _FakeResponse(b'{"ok": true}')

    monkeypatch.setattr(cli_api.urllib.request, "urlopen", fake_urlopen)

    result = cli_api.api_request("GET", "/api/sessions", query={"exclude_thread": "42"})
    assert result == {"ok": True}
    assert captured["url"] == "http://h:1/api/sessions?exclude_thread=42"
    assert captured["method"] == "GET"
    # query-only request has no JSON body
    header_names = {k.lower() for k in captured["headers"]}
    assert "Content-type" not in header_names


def test_api_request_post_with_body_and_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CCDB_API_URL", "http://h:1")
    monkeypatch.setenv("CCDB_API_SECRET", "s3cret")
    captured: dict[str, object] = {}

    def fake_urlopen(req: urllib.request.Request) -> _FakeResponse:
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["data"] = req.data
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _FakeResponse(b'{"status": "posted"}')

    monkeypatch.setattr(cli_api.urllib.request, "urlopen", fake_urlopen)

    result = cli_api.api_request("POST", "/api/lounge", body={"message": "hi", "label": "x"})
    assert result == {"status": "posted"}
    assert captured["method"] == "POST"
    assert json.loads(captured["data"]) == {"message": "hi", "label": "x"}
    assert captured["headers"]["content-type"] == "application/json"
    assert captured["headers"]["authorization"] == "Bearer s3cret"


def test_api_request_empty_body_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CCDB_API_URL", "http://h:1")

    def fake_urlopen(req: urllib.request.Request) -> _FakeResponse:
        return _FakeResponse(b"", status=204)

    monkeypatch.setattr(cli_api.urllib.request, "urlopen", fake_urlopen)
    assert cli_api.api_request("DELETE", "/api/claims", query={"resource": "r"}) is None


def test_api_request_http_error_exits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CCDB_API_URL", "http://h:1")

    def fake_urlopen(req: urllib.request.Request) -> _FakeResponse:
        raise urllib.error.HTTPError(
            req.full_url, 409, "Conflict", {}, io.BytesIO(b'{"error":"held"}')
        )

    monkeypatch.setattr(cli_api.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(SystemExit) as exc:
        cli_api.api_request("POST", "/api/claims", body={"resource": "r"})
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "HTTP 409" in err


# ---------------------------------------------------------------------------
# command handlers — api_request is mocked, we assert the wiring.
# ---------------------------------------------------------------------------


def test_cmd_lounge_post_includes_thread_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCORD_THREAD_ID", "999")
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, *, body=None, query=None):
        captured.update(method=method, path=path, body=body)
        return {"status": "posted"}

    monkeypatch.setattr(cli_api, "api_request", fake_request)

    args = SimpleNamespace(thread_id=None, message="hello", label="relay-upgrader")
    rc = cli_api.cmd_lounge_post(args)
    assert rc == 0
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/lounge"
    assert captured["body"] == {
        "message": "hello",
        "label": "relay-upgrader",
        "thread_id": "999",
    }


def test_cmd_lounge_post_explicit_thread_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISCORD_THREAD_ID", "999")
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, *, body=None, query=None):
        captured.update(body=body)
        return None

    monkeypatch.setattr(cli_api, "api_request", fake_request)

    args = SimpleNamespace(thread_id="1", message="hi", label=None)
    assert cli_api.cmd_lounge_post(args) == 0
    # label None is dropped, thread_id is the explicit value
    assert captured["body"] == {"message": "hi", "thread_id": "1"}


def test_cmd_message_send_queue_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_THREAD_ID", "me")
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, *, body=None, query=None):
        captured.update(method=method, path=path, body=body)
        return {"status": "delivered"}

    monkeypatch.setattr(cli_api, "api_request", fake_request)

    args = SimpleNamespace(
        thread="123",
        text="hi",
        from_thread=None,
        from_env=True,
        mode="queue",
        hop=0,
    )
    rc = cli_api.cmd_message_send(args)
    assert rc == 0
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/threads/123/message"
    assert captured["body"] == {
        "text": "hi",
        "from_thread": "me",
        "mode": "queue",
        "hop": 0,
    }


def test_cmd_message_send_explicit_from_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, *, body=None, query=None):
        captured.update(body=body)
        return None

    monkeypatch.setattr(cli_api, "api_request", fake_request)

    args = SimpleNamespace(
        thread="123",
        text="hi",
        from_thread="888",
        from_env=False,
        mode="interrupt",
        hop=1,
    )
    assert cli_api.cmd_message_send(args) == 0
    assert captured["body"] == {
        "text": "hi",
        "from_thread": "888",
        "mode": "interrupt",
        "hop": 1,
    }


def test_cmd_claim_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_THREAD_ID", "me")
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, *, body=None, query=None):
        captured.update(method=method, path=path, body=body)
        return {"status": "yours"}

    monkeypatch.setattr(cli_api, "api_request", fake_request)

    args = SimpleNamespace(resource="repo:x#issue-1", note="fixing", thread_id=None)
    assert cli_api.cmd_claim_set(args) == 0
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/claims"
    assert captured["body"] == {
        "resource": "repo:x#issue-1",
        "thread_id": "me",
        "note": "fixing",
    }


def test_cmd_claim_release(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_THREAD_ID", "me")
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, *, body=None, query=None):
        captured.update(method=method, path=path, query=query)
        return None

    monkeypatch.setattr(cli_api, "api_request", fake_request)

    args = SimpleNamespace(resource="repo:x#issue-1", thread_id=None)
    assert cli_api.cmd_claim_release(args) == 0
    assert captured["method"] == "DELETE"
    assert captured["path"] == "/api/claims"
    assert captured["query"] == {"resource": "repo:x#issue-1", "thread_id": "me"}


def test_cmd_sessions_exclude_self(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_THREAD_ID", "me")
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, *, body=None, query=None):
        captured.update(method=method, path=path, query=query)
        return []

    monkeypatch.setattr(cli_api, "api_request", fake_request)

    args = SimpleNamespace(exclude_thread=None, exclude_self=True)
    cli_api.cmd_sessions(args)
    assert captured["query"] == {"exclude_thread": "me"}


def test_cmd_sessions_no_exclude(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, *, body=None, query=None):
        captured.update(query=query)
        return []

    monkeypatch.setattr(cli_api, "api_request", fake_request)

    args = SimpleNamespace(exclude_thread=None, exclude_self=False)
    cli_api.cmd_sessions(args)
    assert captured["query"] is None


def test_cmd_thread_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_request(method: str, path: str, *, body=None, query=None):
        captured.update(method=method, path=path, query=query)
        return {"messages": []}

    monkeypatch.setattr(cli_api, "api_request", fake_request)

    args = SimpleNamespace(thread="555", limit=10)
    cli_api.cmd_thread_messages(args)
    assert captured["method"] == "GET"
    assert captured["path"] == "/api/threads/555/messages"
    assert captured["query"] == {"limit": 10}


# ---------------------------------------------------------------------------
# emit
# ---------------------------------------------------------------------------


def test_emit_none_returns_zero_no_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli_api._emit(None) == 0
    assert capsys.readouterr().out == ""


def test_emit_dict_pretty_prints(capsys: pytest.CaptureFixture[str]) -> None:
    cli_api._emit({"a": 1})
    out = capsys.readouterr().out
    assert json.loads(out) == {"a": 1}
