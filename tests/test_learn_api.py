"""End-to-end tests for the learning-method HTTP API (Phase 7).

Drive the whole flow over the FastAPI TestClient — start → interview → build
(SSE) → teach-back — with both engines faked so nothing spawns a subprocess. The
orchestrator's own questions come from a scripted agent; the build stream uses a
separate fake that writes a real file into the session workspace, proving the
build lands where the file tree and Run read it and that a clean build opens the
teach-back gate.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from forgelearn.agents.base import AgentAdapter, RawEvent
from forgelearn.common.types import AgentEvent, EventKind
from forgelearn.config import get_settings
from forgelearn.orchestrator import engine as engine_mod
from forgelearn.orchestrator.store import SessionStore
from forgelearn.server import learn as learn_mod
from forgelearn.server import streams as streams_mod
from forgelearn.server.app import create_app
from forgelearn.server.routes import FILES_PATH


class _ScriptedAgent(AgentAdapter):
    """Returns queued JSON (as narration) for the orchestrator's questions."""

    name = "scripted"

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)

    def run(self, prompt: str, workspace: Path) -> Iterator[RawEvent]:  # pragma: no cover
        raise NotImplementedError

    def run_events(self, prompt: str, workspace: Path) -> Iterator[AgentEvent]:
        text = self._replies.pop(0)
        yield AgentEvent(kind=EventKind.NARRATION, text=text)
        yield AgentEvent(kind=EventKind.DONE, text="ok")


class _BuildAgent(AgentAdapter):
    """A fake builder: writes a runnable main.py and narrates, like a real build."""

    name = "builder"

    def run(self, prompt: str, workspace: Path) -> Iterator[RawEvent]:  # pragma: no cover
        raise NotImplementedError

    def run_events(self, prompt: str, workspace: Path) -> Iterator[AgentEvent]:
        (Path(workspace) / "main.py").write_text("print('it works')\n", encoding="utf-8")
        yield AgentEvent(kind=EventKind.NARRATION, text="Building your bandit…")
        yield AgentEvent(kind=EventKind.FILE_WRITE, text="main.py", path="main.py")
        yield AgentEvent(kind=EventKind.DONE, text="built")


_QUESTIONS = '{"questions": ["Why?", "Know?", "Hours?"]}'
_LADDER = (
    '{"mission": "Learn RL to build a trading agent", "projects": ['
    '{"id": "p1", "you_build": "a bandit", "you_learn": "exploration", "done_when": "beats random"},'
    '{"id": "p2", "you_build": "q-learning", "you_learn": "Bellman", "done_when": "solves FrozenLake"}]}'
)
_PASS = '{"passed": true, "probes": [], "feedback": "Great.", "progress_note": "You built a bandit.", "storage_note": "Space it."}'


def _parse_sse(body: str) -> list[dict]:
    payloads = []
    for frame in body.strip().split("\n\n"):
        for line in frame.splitlines():
            if line.startswith("data: "):
                payloads.append(json.loads(line[len("data: ") :]))
    return payloads


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A TestClient with a fresh store, faked engines, and tmp workspaces."""
    monkeypatch.setattr(get_settings(), "workspace_dir", tmp_path)

    # One shared store across all requests in the test (mirrors the singleton).
    store = SessionStore()
    monkeypatch.setattr(engine_mod, "get_store", lambda: store)

    # The orchestrator's questions come from the scripted agent, in call order.
    scripted = _ScriptedAgent([_QUESTIONS, _LADDER, _PASS])
    monkeypatch.setattr(engine_mod, "get_agent", lambda name: scripted)
    # The build stream uses its own fake that writes a real file.
    monkeypatch.setattr(streams_mod, "get_agent", lambda name: _BuildAgent())

    return TestClient(create_app())


def test_full_learning_flow(client: TestClient) -> None:
    """Start → interview → build → teach-back drives the method end to end."""
    # 1. Start: get interview questions.
    start = client.post(learn_mod.LEARN_START_PATH, json={"topic": "reinforcement learning"})
    assert start.status_code == 200
    session_id = start.json()["id"]
    assert start.json()["stage"] == "interview"
    assert len(start.json()["interview_questions"]) == 3

    # 2. Interview: get the mission + ladder.
    interview = client.post(
        learn_mod.LEARN_INTERVIEW_PATH,
        json={"session": session_id, "answers": ["a job", "basics", "10"]},
    )
    assert interview.status_code == 200
    body = interview.json()
    assert body["stage"] == "ladder"
    assert body["mission"].startswith("Learn RL")
    assert [p["id"] for p in body["projects"]] == ["p1", "p2"]
    assert body["projects"][0]["status"] == "active"

    # 3. Build the first rung (SSE) — a real file lands in the workspace.
    build = client.get(learn_mod.LEARN_BUILD_PATH, params={"session": session_id, "project": "p1"})
    assert build.status_code == 200
    kinds = [p["kind"] for p in _parse_sse(build.text)]
    assert kinds[-1] == "done"

    files = client.get(FILES_PATH, params={"session": session_id}).json()["files"]
    assert any(f["path"] == "main.py" for f in files)

    # The clean build opened the teach-back gate.
    after_build = client.get(learn_mod.LEARN_SESSION_PATH, params={"session": session_id}).json()
    assert after_build["stage"] == "teachback"
    assert next(p for p in after_build["projects"] if p["id"] == "p1")["status"] == "built"

    # 4. Teach-back passes → the next rung unlocks and progress is logged.
    tb = client.post(
        learn_mod.LEARN_TEACHBACK_PATH,
        json={"session": session_id, "project": "p1", "explanation": "epsilon balances explore/exploit"},
    )
    assert tb.status_code == 200
    verdict = tb.json()
    assert verdict["passed"] is True
    assert verdict["next_project_id"] == "p2"
    assert verdict["storage_note"]
    projects = {p["id"]: p["status"] for p in verdict["session"]["projects"]}
    assert projects == {"p1": "complete", "p2": "active"}
    # Baseline + the passing note (day-one comparison).
    assert len(verdict["session"]["progress"]) == 2


def test_sessions_lists_saved_sessions(client: TestClient) -> None:
    """The sessions index lists a started session for a returning learner."""
    session_id = client.post(learn_mod.LEARN_START_PATH, json={"topic": "rl"}).json()["id"]
    listing = client.get(learn_mod.LEARN_SESSIONS_PATH)
    assert listing.status_code == 200
    ids = [s["id"] for s in listing.json()["sessions"]]
    assert session_id in ids


def test_export_returns_downloadable_html(client: TestClient) -> None:
    """After a build, export streams a self-contained HTML attachment."""
    session_id = client.post(learn_mod.LEARN_START_PATH, json={"topic": "rl"}).json()["id"]
    client.post(
        learn_mod.LEARN_INTERVIEW_PATH,
        json={"session": session_id, "answers": ["a", "b", "c"]},
    )
    client.get(learn_mod.LEARN_BUILD_PATH, params={"session": session_id, "project": "p1"})

    resp = client.get(learn_mod.LEARN_EXPORT_PATH, params={"session": session_id})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "attachment" in resp.headers["content-disposition"]
    assert f"forgelearn-{session_id}.html" in resp.headers["content-disposition"]
    # The export embeds the mission and the file the build wrote.
    assert "Learn RL" in resp.text
    assert "main.py" in resp.text
    assert "it works" in resp.text


def test_export_unknown_session_is_a_clean_400(client: TestClient) -> None:
    """Exporting a missing session is a 400 with a message, not a 500."""
    resp = client.get(learn_mod.LEARN_EXPORT_PATH, params={"session": "nope"})
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_unknown_session_is_a_clean_400(client: TestClient) -> None:
    """Fetching a missing session is a 400 with a message, not a 500."""
    resp = client.get(learn_mod.LEARN_SESSION_PATH, params={"session": "nope"})
    assert resp.status_code == 400
    assert "error" in resp.json()


def test_start_requires_a_topic(client: TestClient) -> None:
    """An empty body fails validation (422), not a crash."""
    assert client.post(learn_mod.LEARN_START_PATH, json={}).status_code == 422


def test_locked_rung_build_reports_error_frame(client: TestClient) -> None:
    """Building a locked rung streams a terminal error frame, not a dropped run."""
    session_id = client.post(learn_mod.LEARN_START_PATH, json={"topic": "rl"}).json()["id"]
    client.post(
        learn_mod.LEARN_INTERVIEW_PATH,
        json={"session": session_id, "answers": ["a", "b", "c"]},
    )
    resp = client.get(learn_mod.LEARN_BUILD_PATH, params={"session": session_id, "project": "p2"})
    assert resp.status_code == 200
    (payload,) = _parse_sse(resp.text)
    assert payload["kind"] == "error"
    assert "locked" in payload["text"]
