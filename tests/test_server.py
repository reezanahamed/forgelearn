"""Smoke tests for the FastAPI server, SSE framing, and the agent stream bridge.

These run fully offline: the stream tests swap the real Claude CLI adapter for a
tiny fake so no subprocess is spawned. They assert the wire contract the browser
depends on — JSON status endpoints, a served index page, and ordered SSE frames
that end on a terminal event.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from forgelearn.agents.base import AgentAdapter, RawEvent
from forgelearn.common.errors import AgentError
from forgelearn.common.types import AgentEvent, EventKind
from forgelearn.config import get_settings
from forgelearn.server import streams
from forgelearn.server.app import create_app
from forgelearn.server.routes import (
    AGENTS_PATH,
    FILE_PATH,
    FILE_RAW_PATH,
    FILES_PATH,
    RUN_PATH,
)
from forgelearn.server.sse import error_frame, event_frame, sse_frame


# --- Fakes -------------------------------------------------------------------


class _FakeAgent(AgentAdapter):
    """An adapter that yields a scripted event sequence, no subprocess."""

    name = "fake"

    def __init__(self, events: list[AgentEvent]) -> None:
        self._events = events

    def run(self, prompt: str, workspace: Path) -> Iterator[RawEvent]:  # pragma: no cover
        raise NotImplementedError("fake overrides run_events directly")

    def run_events(self, prompt: str, workspace: Path) -> Iterator[AgentEvent]:
        yield from self._events


class _BoomAgent(AgentAdapter):
    """An adapter whose run fails mid-stream with a typed engine error."""

    name = "boom"

    def run(self, prompt: str, workspace: Path) -> Iterator[RawEvent]:  # pragma: no cover
        raise NotImplementedError

    def run_events(self, prompt: str, workspace: Path) -> Iterator[AgentEvent]:
        yield AgentEvent(kind=EventKind.NARRATION, text="starting")
        raise AgentError("the CLI blew up")


def _parse_sse(body: str) -> list[dict]:
    """Extract the JSON payloads from an SSE response body, in order."""
    payloads = []
    for frame in body.strip().split("\n\n"):
        for line in frame.splitlines():
            if line.startswith("data: "):
                payloads.append(json.loads(line[len("data: ") :]))
    return payloads


@pytest.fixture()
def client() -> TestClient:
    """A TestClient over a freshly built app."""
    return TestClient(create_app())


# --- SSE framing -------------------------------------------------------------


def test_sse_frame_shape() -> None:
    """A frame is one data line terminated by a blank line."""
    frame = sse_frame({"kind": "done"})
    assert frame == 'data: {"kind": "done"}\n\n'


def test_event_frame_roundtrips_an_agent_event() -> None:
    """An AgentEvent serializes to a JSON payload carrying its kind and text."""
    frame = event_frame(AgentEvent(kind=EventKind.FILE_WRITE, text="hello.py"))
    (payload,) = _parse_sse(frame)
    assert payload["kind"] == "file_write"
    assert payload["text"] == "hello.py"


def test_error_frame_is_a_terminal_error() -> None:
    """error_frame produces an error-kind payload with is_error set."""
    (payload,) = _parse_sse(error_frame("nope"))
    assert payload["kind"] == "error"
    assert payload["is_error"] is True
    assert payload["text"] == "nope"


# --- JSON + index routes -----------------------------------------------------


def test_health(client: TestClient) -> None:
    """Health reports ok, the version, and the registered agents."""
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["default_agent"] == "claude"
    assert "claude" in body["agents"]


def test_agents_endpoint(client: TestClient) -> None:
    """The agents endpoint lists both providers for the Phase 6 dropdown."""
    body = client.get("/api/agents").json()
    assert "claude" in body["agents"]
    assert "codex" in body["agents"]
    assert body["default_agent"] == "claude"


def test_index_served(client: TestClient) -> None:
    """The root path serves the chat shell + workspace panel and links its assets."""
    resp = client.get("/")
    assert resp.status_code == 200
    assert "ForgeLearn" in resp.text
    assert 'id="chat"' in resp.text  # the conversation container
    assert 'id="workspace"' in resp.text  # Phase 5 workspace panel
    assert 'id="run"' in resp.text  # the Run button
    # Phase 4 split behaviour/styling into modular static assets the page links.
    assert "/static/app.js" in resp.text
    assert "/static/styles.css" in resp.text


def test_static_assets_served(client: TestClient) -> None:
    """The learning UI's JS and CSS are served from the /static mount."""
    js = client.get("/static/app.js")
    assert js.status_code == 200
    # Phase 7: the page drives the learning method (start → interview → build →
    # teach-back) and builds a rung over SSE via the orchestrator's build endpoint.
    assert "EventSource" in js.text
    assert "/api/learn/start" in js.text
    assert "/api/learn/interview" in js.text
    assert "/api/learn/build" in js.text
    assert "/api/learn/teachback" in js.text
    # It still drives the workspace (files + run) with the session id.
    assert RUN_PATH in js.text
    assert FILES_PATH in js.text

    # Phase 6: the page has a provider dropdown and passes the choice through.
    assert AGENTS_PATH in js.text
    assert 'id="agent"' in client.get("/").text

    css = client.get("/static/styles.css")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    # Assets are served no-cache so a `git pull` update reaches the browser.
    assert "no-cache" in js.headers.get("cache-control", "")
    assert "no-cache" in css.headers.get("cache-control", "")


# --- SSE stream endpoint (bridge) -------------------------------------------


def test_stream_yields_ordered_event_frames(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stream endpoint emits one SSE frame per agent event, in order."""
    scripted = [
        AgentEvent(kind=EventKind.NARRATION, text="I'll build it"),
        AgentEvent(kind=EventKind.FILE_WRITE, text="app.py", path="app.py"),
        AgentEvent(kind=EventKind.DONE, text="run complete"),
    ]
    monkeypatch.setattr(streams, "get_agent", lambda name: _FakeAgent(scripted))
    monkeypatch.setattr(streams, "create_ephemeral", lambda: Path("."))

    resp = client.get("/api/stream", params={"prompt": "build an app"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    kinds = [p["kind"] for p in _parse_sse(resp.text)]
    assert kinds == ["narration", "file_write", "done"]


def test_stream_converts_engine_error_to_terminal_frame(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typed engine failure becomes a terminal error frame, not a 500."""
    monkeypatch.setattr(streams, "get_agent", lambda name: _BoomAgent())
    monkeypatch.setattr(streams, "create_ephemeral", lambda: Path("."))

    resp = client.get("/api/stream", params={"prompt": "explode please"})
    assert resp.status_code == 200

    payloads = _parse_sse(resp.text)
    assert payloads[0]["kind"] == "narration"  # partial output still delivered
    assert payloads[-1]["kind"] == "error"
    assert "blew up" in payloads[-1]["text"]


def test_stream_requires_a_prompt(client: TestClient) -> None:
    """Omitting the prompt is a validation error, not a crash."""
    assert client.get("/api/stream").status_code == 422


# --- Workspace: files, viewer, and Run (Phase 5) ----------------------------


@pytest.fixture()
def ws_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the workspace directory to a temp dir for the endpoint tests."""
    monkeypatch.setattr(get_settings(), "workspace_dir", tmp_path)
    return tmp_path


def _seed_project(session: str, filename: str, body: str) -> None:
    """Write a file into ``session``'s workspace, as a build would have."""
    from forgelearn.workspace import get_or_create

    (get_or_create(session) / filename).write_text(body, encoding="utf-8")


def test_files_endpoint_lists_built_project(client: TestClient, ws_root: Path) -> None:
    """After files exist in a session workspace, /api/files lists them."""
    _seed_project("sess01", "main.py", "print('hi')\n")
    body = client.get(FILES_PATH, params={"session": "sess01"}).json()
    assert [f["path"] for f in body["files"]] == ["main.py"]
    assert body["files"][0]["size"] > 0


def test_files_endpoint_rejects_bad_session(client: TestClient, ws_root: Path) -> None:
    """An invalid session id is a 400, not a traversal."""
    resp = client.get(FILES_PATH, params={"session": "../etc"})
    assert resp.status_code == 400


def test_file_endpoint_returns_contents(client: TestClient, ws_root: Path) -> None:
    """/api/file returns a workspace file's text for the viewer."""
    _seed_project("sess02", "hello.py", "print('hello')\n")
    body = client.get(
        FILE_PATH, params={"session": "sess02", "path": "hello.py"}
    ).json()
    assert body["content"] == "print('hello')\n"


def test_file_endpoint_rejects_traversal(client: TestClient, ws_root: Path) -> None:
    """A path escaping the workspace is a 400."""
    _seed_project("sess03", "ok.py", "x = 1")
    resp = client.get(
        FILE_PATH, params={"session": "sess03", "path": "../../secret"}
    )
    assert resp.status_code == 400


def test_file_raw_serves_bytes_with_type(client: TestClient, ws_root: Path) -> None:
    """/api/file/raw returns the exact bytes with a guessed content type."""
    from forgelearn.workspace import get_or_create

    raw = b"\x89PNG\r\n\x1a\nnot-a-real-image-but-bytes"
    (get_or_create("sess0img") / "plot.png").write_bytes(raw)
    resp = client.get(FILE_RAW_PATH, params={"session": "sess0img", "path": "plot.png"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/png")
    assert resp.content == raw


def test_file_raw_rejects_traversal(client: TestClient, ws_root: Path) -> None:
    """A path escaping the workspace is a 400 on the raw endpoint too."""
    _seed_project("sess0trav", "ok.py", "x = 1")
    resp = client.get(FILE_RAW_PATH, params={"session": "sess0trav", "path": "../../secret"})
    assert resp.status_code == 400


def test_run_endpoint_streams_output(client: TestClient, ws_root: Path) -> None:
    """/api/run executes the built project and streams command→output→done."""
    _seed_project("sess04", "main.py", "print('it works')\n")
    resp = client.get(RUN_PATH, params={"session": "sess04"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    payloads = _parse_sse(resp.text)
    assert payloads[0]["kind"] == "command"
    assert any(
        p["kind"] == "tool_result" and p["text"] == "it works" for p in payloads
    )
    assert payloads[-1]["kind"] == "done"


def test_run_endpoint_reports_nothing_to_run(client: TestClient, ws_root: Path) -> None:
    """Running a workspace with no code yields a terminal error frame, not a 500."""
    from forgelearn.workspace import get_or_create

    get_or_create("sess05")  # exists but empty
    resp = client.get(RUN_PATH, params={"session": "sess05"})
    assert resp.status_code == 200
    (payload,) = _parse_sse(resp.text)
    assert payload["kind"] == "error"
    assert "nothing to run" in payload["text"]


def test_stream_routes_to_the_selected_provider(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dropdown's `agent=` choice selects which adapter the bridge runs."""
    picked: list[str] = []

    def _spy_get_agent(name: str) -> AgentAdapter:
        picked.append(name)
        return _FakeAgent([AgentEvent(kind=EventKind.DONE, text="ok")])

    monkeypatch.setattr(streams, "get_agent", _spy_get_agent)
    monkeypatch.setattr(streams, "create_ephemeral", lambda: Path("."))

    resp = client.get("/api/stream", params={"prompt": "build it", "agent": "codex"})
    assert resp.status_code == 200
    assert picked == ["codex"]  # the request was routed to the chosen provider


def test_stream_defaults_provider_when_unspecified(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting `agent` falls back to the configured default provider."""
    picked: list[str] = []

    def _spy_get_agent(name: str) -> AgentAdapter:
        picked.append(name)
        return _FakeAgent([AgentEvent(kind=EventKind.DONE, text="ok")])

    monkeypatch.setattr(streams, "get_agent", _spy_get_agent)
    monkeypatch.setattr(streams, "create_ephemeral", lambda: Path("."))

    client.get("/api/stream", params={"prompt": "build it"})
    assert picked == [get_settings().default_agent]


def test_unknown_agent_is_reported_in_stream(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown provider surfaces as an error frame from the real factory."""
    monkeypatch.setattr(streams, "create_ephemeral", lambda: Path("."))
    resp = client.get(
        "/api/stream", params={"prompt": "hi", "agent": "does-not-exist"}
    )
    assert resp.status_code == 200
    (payload,) = _parse_sse(resp.text)
    assert payload["kind"] == "error"
    assert "unknown agent" in payload["text"]
