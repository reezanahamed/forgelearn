"""Offline tests for the guided-lesson orchestrator (redesign, Phase A).

Drive the whole new loop, interview -> syllabus -> open lesson (teach) -> check ->
demo -> learner build -> review, with a scripted fake agent, so no subprocess or
real CLI runs. The fake yields its canned JSON as narration, exercising the real
``complete()`` + JSON parse + transition path end to end.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from forgelearn.agents.base import AgentAdapter, RawEvent
from forgelearn.common.errors import OrchestratorError
from forgelearn.common.types import AgentEvent, EventKind, LessonStage, ProjectStatus, SessionStage
from forgelearn.config import get_settings
from forgelearn.orchestrator.course_engine import CourseOrchestrator
from forgelearn.orchestrator.store import SessionStore


class ScriptedAgent(AgentAdapter):
    """Replies with queued text (as narration), in call order."""

    name = "scripted"

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []

    def run(self, prompt: str, workspace: Path) -> Iterator[RawEvent]:  # pragma: no cover
        raise NotImplementedError

    def run_events(self, prompt: str, workspace: Path) -> Iterator[AgentEvent]:
        self.prompts.append(prompt)
        assert self._replies, "ScriptedAgent ran out of replies"
        yield AgentEvent(kind=EventKind.NARRATION, text=self._replies.pop(0))
        yield AgentEvent(kind=EventKind.DONE, text="ok")


_QUESTIONS = '{"questions": ["Why?", "What do you know?", "Hours per week?"]}'
_SYLLABUS = (
    '{"mission": "Understand reinforcement learning by building small agents",'
    ' "lessons": ['
    '{"id": "u1", "title": "Reward and the RL loop", "goal": "explain reward",'
    ' "domain_type": "code", "demo_task": "a tiny reward counter", "build_task": "your own reward counter"},'
    '{"id": "u2", "title": "Exploration vs exploitation", "goal": "explain the tradeoff",'
    ' "domain_type": "code", "demo_task": "an epsilon-greedy pick", "build_task": "your own epsilon-greedy pick"}]}'
)
_CONTENT = (
    '{"concept": "Reward is a number the agent gets for an action.",'
    ' "widget": {"title": "Try it", "caption": "drag the slider",'
    ' "html": "<!doctype html><body>slider</body></html>"},'
    ' "check": {"question": "What is reward?", "kind": "mcq", "options": ["a number", "a color"]}}'
)
_CHECK_OK = '{"correct": true, "feedback": "Nice.", "explanation": "Reward is the signal the agent maximizes."}'
_REVIEW_FAIL = '{"passed": false, "feedback": "Close.", "hints": ["Return the total, not the last value."], "progress_note": ""}'
_REVIEW_PASS = '{"passed": true, "feedback": "Great.", "hints": [], "progress_note": "You can now track reward over time."}'
_HINT = '{"hint": "Start by making a variable called total set to zero."}'


@pytest.fixture()
def store() -> SessionStore:
    return SessionStore()


@pytest.fixture(autouse=True)
def _tmp_workspaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "workspace_dir", tmp_path)


def _orc(store: SessionStore, replies: list[str]) -> tuple[CourseOrchestrator, ScriptedAgent]:
    agent = ScriptedAgent(replies)
    return CourseOrchestrator(agent=agent, store=store), agent


def _to_syllabus(store: SessionStore, extra: list[str]) -> tuple[CourseOrchestrator, ScriptedAgent, str]:
    orc, agent = _orc(store, [_QUESTIONS, _SYLLABUS, *extra])
    session = orc.start("reinforcement learning")
    session = orc.submit_interview(session.id, ["a", "b", "c"])
    return orc, agent, session.id


# --- Interview + syllabus ----------------------------------------------------


def test_start_generates_interview_questions(store: SessionStore) -> None:
    orc, _ = _orc(store, [_QUESTIONS])
    session = orc.start("reinforcement learning")
    assert session.stage is SessionStage.INTERVIEW
    assert len(session.interview_questions) == 3


def test_submit_interview_builds_mission_and_syllabus(store: SessionStore) -> None:
    orc, _, sid = _to_syllabus(store, [])
    session = store.get(sid)
    assert session.stage is SessionStage.SYLLABUS
    assert session.mission.startswith("Understand reinforcement learning")
    assert [les.id for les in session.lessons] == ["u1", "u2"]
    assert session.lessons[0].status is ProjectStatus.ACTIVE
    assert session.lessons[1].status is ProjectStatus.LOCKED
    assert session.active_lesson_id == "u1"
    assert "Day one" in session.progress[0].note


# --- Open a lesson (teach) ---------------------------------------------------


def test_open_lesson_generates_teaching_content(store: SessionStore) -> None:
    orc, _, sid = _to_syllabus(store, [_CONTENT])
    session = orc.open_lesson(sid, "u1")
    lesson = session.lesson("u1")
    assert session.stage is SessionStage.LEARNING
    assert lesson.stage is LessonStage.LEARN
    assert lesson.concept.startswith("Reward is a number")
    assert lesson.widget and lesson.widget.html.startswith("<!doctype html>")
    assert lesson.check and lesson.check.kind == "mcq"


def test_open_locked_lesson_is_rejected(store: SessionStore) -> None:
    orc, _, sid = _to_syllabus(store, [])
    with pytest.raises(OrchestratorError):
        orc.open_lesson(sid, "u2")  # still locked


def test_reopening_a_lesson_does_not_regenerate(store: SessionStore) -> None:
    """Content is generated once and cached (no second engine call)."""
    orc, agent, sid = _to_syllabus(store, [_CONTENT])
    orc.open_lesson(sid, "u1")
    calls = len(agent.prompts)
    orc.open_lesson(sid, "u1")  # no new reply queued; must not call the agent again
    assert len(agent.prompts) == calls


# --- Check -------------------------------------------------------------------


def test_submit_check_returns_verdict_and_advances_to_demo(store: SessionStore) -> None:
    orc, _, sid = _to_syllabus(store, [_CONTENT, _CHECK_OK])
    orc.open_lesson(sid, "u1")
    result = orc.submit_check(sid, "u1", "it is a number")
    assert result.correct is True
    assert result.explanation
    assert store.get(sid).lesson("u1").stage is LessonStage.DEMO


# --- Demo + build ------------------------------------------------------------


def test_demo_instruction_is_grounded(store: SessionStore) -> None:
    orc, _, sid = _to_syllabus(store, [_CONTENT])
    orc.open_lesson(sid, "u1")
    prompt = orc.demo_instruction(sid, "u1")
    assert "a tiny reward counter" in prompt  # the demo_task
    assert "Understand reinforcement learning" in prompt  # grounded in the mission


def test_mark_demo_built_opens_learner_build(store: SessionStore) -> None:
    orc, _, sid = _to_syllabus(store, [_CONTENT])
    orc.open_lesson(sid, "u1")
    orc.demo_instruction(sid, "u1")
    session = orc.mark_demo_built(sid, "u1")
    assert session.lesson("u1").stage is LessonStage.BUILD


def test_build_fail_keeps_lesson_and_returns_hints(store: SessionStore) -> None:
    orc, _, sid = _to_syllabus(store, [_CONTENT, _REVIEW_FAIL])
    orc.open_lesson(sid, "u1")
    orc.mark_demo_built(sid, "u1")
    result = orc.submit_build(sid, "u1", "main.py:\nprint(1)\n")
    assert result.passed is False
    assert result.hints
    session = store.get(sid)
    assert session.lesson("u1").status is ProjectStatus.ACTIVE  # not complete
    assert session.lesson("u2").status is ProjectStatus.LOCKED


def test_build_pass_unlocks_next_and_logs_progress(store: SessionStore) -> None:
    orc, _, sid = _to_syllabus(store, [_CONTENT, _REVIEW_PASS])
    orc.open_lesson(sid, "u1")
    orc.mark_demo_built(sid, "u1")
    result = orc.submit_build(sid, "u1", "main.py:\nprint('total', total)\n")
    assert result.passed is True
    assert result.next_lesson_id == "u2"
    session = store.get(sid)
    assert session.lesson("u1").status is ProjectStatus.COMPLETE
    assert session.lesson("u2").status is ProjectStatus.ACTIVE
    assert session.active_lesson_id == "u2"
    assert len(session.progress) == 2  # baseline + the pass note


def test_last_lesson_pass_completes_course(store: SessionStore) -> None:
    orc, _, sid = _to_syllabus(
        store, [_CONTENT, _REVIEW_PASS, _CONTENT, _REVIEW_PASS]
    )
    orc.open_lesson(sid, "u1")
    orc.mark_demo_built(sid, "u1")
    orc.submit_build(sid, "u1", "x")
    orc.open_lesson(sid, "u2")
    orc.mark_demo_built(sid, "u2")
    result = orc.submit_build(sid, "u2", "x")
    assert result.next_lesson_id is None
    assert result.stage is SessionStage.COMPLETE
    assert all(les.status is ProjectStatus.COMPLETE for les in store.get(sid).lessons)


# --- Hints -------------------------------------------------------------------


def test_get_hint_returns_a_nudge(store: SessionStore) -> None:
    orc, _, sid = _to_syllabus(store, [_CONTENT, _HINT])
    orc.open_lesson(sid, "u1")
    orc.mark_demo_built(sid, "u1")
    hint = orc.get_hint(sid, "u1", "main.py:\n# empty\n")
    assert "total" in hint


# --- Grade + plain text ------------------------------------------------------


def test_grade_flows_and_is_stored(store: SessionStore) -> None:
    orc, agent = _orc(store, [_QUESTIONS])
    session = orc.start("rl", grade=5)
    assert session.reading_grade == 5
    assert "GRADE 5" in agent.prompts[0]


def test_generated_prose_is_stripped_of_em_dashes(store: SessionStore) -> None:
    dashed_syllabus = (
        '{"mission": "Learn RL — fast", "lessons": [{"id": "u1", "title": "Reward — basics",'
        ' "goal": "the — idea", "domain_type": "code", "demo_task": "a — demo", "build_task": "your — build"}]}'
    )
    orc, _ = _orc(store, [_QUESTIONS, dashed_syllabus])
    session = orc.start("rl")
    session = orc.submit_interview(session.id, ["a", "b", "c"])
    assert "—" not in session.mission
    assert "—" not in session.lessons[0].title
    assert "—" not in session.lessons[0].goal


def test_widget_html_is_preserved_verbatim(store: SessionStore) -> None:
    """Widget HTML is code, not prose: it must not be dash-mangled."""
    content = (
        '{"concept": "ok", "widget": {"title": "t", "caption": "c",'
        ' "html": "<!doctype html><style>a{margin:0 — 2px}</style><body>x</body>"},'
        ' "check": {"question": "q?", "kind": "short", "options": []}}'
    )
    orc, _, sid = _to_syllabus(store, [content])
    session = orc.open_lesson(sid, "u1")
    assert "—" in session.lesson("u1").widget.html  # preserved exactly
