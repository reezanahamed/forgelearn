"""End-to-end tests for the guided-lesson HTTP API (redesign, Phase B).

Drive the whole new flow over the FastAPI TestClient, start -> interview -> open
lesson -> check -> demo (SSE) -> save a file -> build review -> hint, with both
engines faked so nothing spawns a subprocess. Proves the routes wire the course
orchestrator correctly and that the build review reads the learner's real files.
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
from forgelearn.orchestrator import course_engine as ce
from forgelearn.orchestrator.store import SessionStore
from forgelearn.server import course as course_mod
from forgelearn.server import streams as streams_mod
from forgelearn.server.app import create_app
from forgelearn.server.routes import FILE_SAVE_PATH, FILES_PATH


class _ScriptedAgent(AgentAdapter):
    name = "scripted"

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)

    def run(self, prompt: str, workspace: Path) -> Iterator[RawEvent]:  # pragma: no cover
        raise NotImplementedError

    def run_events(self, prompt: str, workspace: Path) -> Iterator[AgentEvent]:
        yield AgentEvent(kind=EventKind.NARRATION, text=self._replies.pop(0))
        yield AgentEvent(kind=EventKind.DONE, text="ok")


class _DemoAgent(AgentAdapter):
    """A fake demo builder: writes a runnable worked example, like a real demo."""

    name = "demo"

    def run(self, prompt: str, workspace: Path) -> Iterator[RawEvent]:  # pragma: no cover
        raise NotImplementedError

    def run_events(self, prompt: str, workspace: Path) -> Iterator[AgentEvent]:
        (Path(workspace) / "example.py").write_text("print('reward', 1)\n", encoding="utf-8")
        yield AgentEvent(kind=EventKind.NARRATION, text="Building the worked example...")
        yield AgentEvent(kind=EventKind.FILE_WRITE, text="example.py", path="example.py")
        yield AgentEvent(kind=EventKind.DONE, text="built")


_QUESTIONS = '{"questions": ["Why?", "Know?", "Hours?"]}'
_SYLLABUS = (
    '{"mission": "Understand RL by building small agents", "lessons": ['
    '{"id": "u1", "title": "Reward", "goal": "explain reward", "domain_type": "code",'
    ' "demo_task": "a reward counter", "build_task": "your own reward counter"},'
    '{"id": "u2", "title": "Exploration", "goal": "the tradeoff", "domain_type": "code",'
    ' "demo_task": "epsilon-greedy", "build_task": "your epsilon-greedy"}]}'
)
_CONTENT = (
    '{"concept": "Reward is a number for an action.",'
    ' "widget": {"title": "Try", "caption": "drag", "html": "<!doctype html><body>w</body></html>"},'
    ' "check": {"question": "What is reward?", "kind": "mcq", "options": ["a number", "a color"]}}'
)
_CHECK_OK = '{"correct": true, "feedback": "Yes.", "explanation": "It is the signal to maximize."}'
_REVIEW_PASS = '{"passed": true, "feedback": "Great.", "hints": [], "progress_note": "You can track reward now."}'
_HINT = '{"hint": "Make a variable total set to zero."}'


def _parse_sse(body: str) -> list[dict]:
    out = []
    for frame in body.strip().split("\n\n"):
        for line in frame.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[len("data: ") :]))
    return out


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(get_settings(), "workspace_dir", tmp_path)
    store = SessionStore()
    monkeypatch.setattr(ce, "get_store", lambda: store)
    scripted = _ScriptedAgent([_QUESTIONS, _SYLLABUS, _CONTENT, _CHECK_OK, _REVIEW_PASS, _HINT])
    monkeypatch.setattr(ce, "get_agent", lambda name: scripted)
    monkeypatch.setattr(streams_mod, "get_agent", lambda name: _DemoAgent())
    return TestClient(create_app())


def test_full_course_flow(client: TestClient) -> None:
    # 1. Start -> interview questions
    start = client.post(course_mod.COURSE_START_PATH, json={"topic": "reinforcement learning"})
    assert start.status_code == 200
    sid = start.json()["id"]
    assert start.json()["stage"] == "interview"

    # 2. Interview -> mission + syllabus
    interview = client.post(
        course_mod.COURSE_INTERVIEW_PATH, json={"session": sid, "answers": ["a", "b", "c"]}
    )
    body = interview.json()
    assert body["stage"] == "syllabus"
    assert [le["id"] for le in body["lessons"]] == ["u1", "u2"]
    assert body["lessons"][0]["status"] == "active"

    # 3. Open lesson u1 -> concept + widget + check generated
    opened = client.post(course_mod.COURSE_OPEN_PATH, json={"session": sid, "lesson": "u1"}).json()
    u1 = next(le for le in opened["lessons"] if le["id"] == "u1")
    assert u1["concept"].startswith("Reward is a number")
    assert u1["widget"]["html"].startswith("<!doctype html>")
    assert u1["check"]["kind"] == "mcq"
    assert opened["stage"] == "learning"

    # 4. Check -> verdict
    check = client.post(
        course_mod.COURSE_CHECK_PATH, json={"session": sid, "lesson": "u1", "answer": "a number"}
    ).json()
    assert check["correct"] is True
    assert check["explanation"]

    # 5. Demo (SSE) -> a worked example file lands, demo gets marked built
    demo = client.get(course_mod.COURSE_DEMO_PATH, params={"session": sid, "lesson": "u1"})
    assert demo.status_code == 200
    assert _parse_sse(demo.text)[-1]["kind"] == "done"
    files = client.get(FILES_PATH, params={"session": sid}).json()["files"]
    assert any(f["path"] == "example.py" for f in files)

    # 6. Learner saves their own file
    saved = client.post(
        FILE_SAVE_PATH,
        json={"session": sid, "path": "mine.py", "content": "total = 0\nprint(total)\n"},
    )
    assert saved.status_code == 200
    assert saved.json()["saved"] == "mine.py"

    # 7. Build review reads the learner's files, passes, unlocks u2
    build = client.post(course_mod.COURSE_BUILD_PATH, json={"session": sid, "lesson": "u1"}).json()
    assert build["passed"] is True
    assert build["next_lesson_id"] == "u2"
    statuses = {le["id"]: le["status"] for le in build["session"]["lessons"]}
    assert statuses == {"u1": "complete", "u2": "active"}

    # 8. Hint
    hint = client.post(course_mod.COURSE_HINT_PATH, json={"session": sid, "lesson": "u2"}).json()
    assert "total" in hint["hint"]


def test_save_file_rejects_traversal(client: TestClient) -> None:
    sid = client.post(course_mod.COURSE_START_PATH, json={"topic": "rl"}).json()["id"]
    resp = client.post(
        FILE_SAVE_PATH, json={"session": sid, "path": "../../escape.py", "content": "x"}
    )
    assert resp.status_code == 400


def test_open_unknown_session_is_clean_400(client: TestClient) -> None:
    resp = client.post(course_mod.COURSE_OPEN_PATH, json={"session": "nope", "lesson": "u1"})
    assert resp.status_code == 400
    assert "error" in resp.json()
