"""The guided-lesson orchestrator (redesign, Phase A).

The new learning method as a state machine. Where the old flow was
interview -> ladder -> the AI builds -> teach-back, the new flow teaches first and
makes the learner build:

    start (interview)
      -> submit_interview  (mission + a SYLLABUS of lessons)
      -> per lesson:
           open_lesson     (generate concept + interactive widget + a check)
           submit_check    (judge the quick understanding question, kindly)
           demo_instruction (the AI builds a worked example, watched live)
           mark_demo_built  (open the learner's own build)
           submit_build     (review the learner's version; pass unlocks the next)
           get_hint         (a single nudge on request, never the answer)

The class stays thin: the teaching lives in :mod:`lesson_prompts`, the transport in
the agents layer, persistence behind the store. It reuses the same
``AgentAdapter.complete`` + JSON parsing the old orchestrator uses, and is pure of
workspace I/O: the server passes the learner's files in as a text summary, so the
whole method is unit-testable with a scripted agent.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from forgelearn.agents import get_agent
from forgelearn.agents.base import AgentAdapter
from forgelearn.common.errors import OrchestratorError
from forgelearn.common.logging import get_logger
from forgelearn.common.types import (
    Check,
    Lesson,
    LessonStage,
    ProgressEntry,
    ProjectStatus,
    Session,
    SessionStage,
    Widget,
)
from forgelearn.config import get_settings
from forgelearn.orchestrator.parsing import extract_json
from forgelearn.orchestrator.prompts import interview_prompt
from forgelearn.orchestrator.lesson_prompts import (
    build_review_prompt,
    check_judge_prompt,
    demo_prompt,
    hint_prompt,
    lesson_content_prompt,
    syllabus_prompt,
)
from forgelearn.orchestrator.store import SessionStore, get_store
from forgelearn.workspace import create_ephemeral

_logger = get_logger("orchestrator.course")

_MAX_TOPIC_CHARS = 500
_MAX_ANSWER_CHARS = 5_000
_MIN_GRADE, _MAX_GRADE = 1, 20


@dataclass
class CheckResult:
    """The outcome of grading a lesson's understanding check."""

    correct: bool
    feedback: str = ""
    explanation: str = ""
    stage: LessonStage = LessonStage.CHECK


@dataclass
class BuildResult:
    """The outcome of reviewing the learner's own build."""

    passed: bool
    feedback: str = ""
    hints: list[str] = field(default_factory=list)
    progress_note: str = ""
    next_lesson_id: str | None = None
    stage: SessionStage = SessionStage.LEARNING


class CourseOrchestrator:
    """Drives one learner through the guided-lesson loop (injectable for tests)."""

    def __init__(
        self, agent: AgentAdapter | None = None, store: SessionStore | None = None
    ) -> None:
        self._agent = agent
        self._store = store or get_store()

    # --- Stage 1: interview --------------------------------------------------

    def start(self, topic: str, grade: int | None = None) -> Session:
        """Begin a session: capture the topic and ask the interview questions."""
        topic = _clean(topic, _MAX_TOPIC_CHARS, "topic")
        settings = get_settings()
        grade = _clamp_grade(grade if grade is not None else settings.reading_grade_default)
        prompt = interview_prompt(
            topic, settings.interview_min_questions, settings.interview_max_questions, grade
        )
        data = self._ask_json(prompt)
        questions = _plain_list(_string_list(data.get("questions"), "questions"))
        if not questions:
            raise OrchestratorError("the engine returned no interview questions")
        session = Session(
            id=uuid.uuid4().hex,
            topic=topic,
            stage=SessionStage.INTERVIEW,
            interview_questions=questions,
            reading_grade=grade,
        )
        _logger.info("course started %s for %r (grade %d)", session.id, topic[:80], grade)
        return self._store.create(session)

    # --- Stage 2: mission + syllabus ----------------------------------------

    def submit_interview(
        self, session_id: str, answers: list[str], grade: int | None = None
    ) -> Session:
        """Take interview answers and generate the mission + syllabus of lessons."""
        session = self._store.get(session_id)
        _require_stage(session, SessionStage.INTERVIEW)
        grade = _apply_grade(session, grade)

        qa_pairs = _zip_qa(session.interview_questions, answers)
        settings = get_settings()
        prompt = syllabus_prompt(
            session.topic,
            qa_pairs,
            settings.ladder_min_projects,
            settings.ladder_max_projects,
            grade,
        )
        data = self._ask_json(prompt)

        mission = _plain_text(str(data.get("mission", "")).strip())
        if not mission:
            raise OrchestratorError("the engine returned an empty mission")
        lessons = _parse_lessons(data.get("lessons"))

        lessons[0].status = ProjectStatus.ACTIVE
        session.mission = mission
        session.lessons = lessons
        session.active_lesson_id = lessons[0].id
        session.stage = SessionStage.SYLLABUS
        session.progress.append(
            ProgressEntry(on=_today(), note=_plain_text(f"Day one, mission: {mission}."))
        )
        _logger.info("course %s: syllabus of %d lessons", session.id, len(lessons))
        return self._store.save(session)

    # --- Stage 3: open a lesson (teach) -------------------------------------

    def open_lesson(
        self, session_id: str, lesson_id: str | None = None, grade: int | None = None
    ) -> Session:
        """Generate a lesson's concept + interactive widget + check; enter it."""
        session = self._store.get(session_id)
        lesson = self._resolve_lesson(session, lesson_id)
        if lesson.status is ProjectStatus.LOCKED:
            raise OrchestratorError(
                f"lesson {lesson.id!r} is locked; finish the earlier lesson first"
            )
        grade = _apply_grade(session, grade)

        if not lesson.concept:  # generate teaching content once, then cache it
            data = self._ask_json(lesson_content_prompt(session.mission, lesson, grade))
            lesson.concept = _plain_text(str(data.get("concept", "")).strip())
            lesson.widget = _parse_widget(data.get("widget"))
            lesson.check = _parse_check(data.get("check"))

        lesson.stage = LessonStage.LEARN
        lesson.status = ProjectStatus.ACTIVE
        session.active_lesson_id = lesson.id
        session.stage = SessionStage.LEARNING
        _logger.info("course %s: opened lesson %s", session.id, lesson.id)
        return self._store.save(session)

    # --- Stage 4: the understanding check -----------------------------------

    def submit_check(
        self, session_id: str, lesson_id: str | None, answer: str, grade: int | None = None
    ) -> CheckResult:
        """Grade the learner's check answer kindly; advance to the demo."""
        answer = _clean(answer, _MAX_ANSWER_CHARS, "answer")
        session = self._store.get(session_id)
        lesson = self._resolve_lesson(session, lesson_id)
        grade = _apply_grade(session, grade)
        question = lesson.check.question if lesson.check else ""

        data = self._ask_json(check_judge_prompt(lesson, question, answer, grade))
        result = CheckResult(
            correct=bool(data.get("correct", False)),
            feedback=_plain_text(str(data.get("feedback", "")).strip()),
            explanation=_plain_text(str(data.get("explanation", "")).strip()),
        )
        # A check teaches; it never blocks. Move on to watch the worked example.
        lesson.stage = LessonStage.DEMO
        result.stage = lesson.stage
        self._store.save(session)
        _logger.info("course %s: checked lesson %s (correct=%s)", session.id, lesson.id, result.correct)
        return result

    # --- Stage 5: the AI demo (build prompt for the stream) -----------------

    def demo_instruction(
        self, session_id: str, lesson_id: str | None = None, grade: int | None = None
    ) -> str:
        """Return the build prompt for the worked example the learner watches."""
        session = self._store.get(session_id)
        lesson = self._resolve_lesson(session, lesson_id)
        grade = _apply_grade(session, grade)
        lesson.stage = LessonStage.DEMO
        session.active_lesson_id = lesson.id
        self._store.save(session)
        return demo_prompt(session.mission, lesson, grade)

    def mark_demo_built(self, session_id: str, lesson_id: str | None = None) -> Session:
        """Record that the worked example finished; open the learner's own build."""
        session = self._store.get(session_id)
        lesson = self._resolve_lesson(session, lesson_id)
        if lesson.status is not ProjectStatus.COMPLETE:
            lesson.stage = LessonStage.BUILD
            session.active_lesson_id = lesson.id
        _logger.info("course %s: demo built for lesson %s", session.id, lesson.id)
        return self._store.save(session)

    # --- Stage 6: the learner's own build + review --------------------------

    def submit_build(
        self,
        session_id: str,
        lesson_id: str | None,
        files_summary: str,
        grade: int | None = None,
    ) -> BuildResult:
        """Review the learner's own build; a pass unlocks the next lesson."""
        session = self._store.get(session_id)
        lesson = self._resolve_lesson(session, lesson_id)
        grade = _apply_grade(session, grade)

        data = self._ask_json(
            build_review_prompt(session.mission, lesson, files_summary or "(no files yet)", grade)
        )
        passed = bool(data.get("passed", False))
        result = BuildResult(
            passed=passed,
            feedback=_plain_text(str(data.get("feedback", "")).strip()),
            hints=_plain_list(_string_list(data.get("hints"), "hints")),
            progress_note=_plain_text(str(data.get("progress_note", "")).strip()),
        )

        if passed:
            lesson.status = ProjectStatus.COMPLETE
            lesson.stage = LessonStage.DONE
            if result.progress_note:
                session.progress.append(ProgressEntry(on=_today(), note=result.progress_note))
            result.next_lesson_id = self._unlock_next(session, lesson)
            session.active_lesson_id = result.next_lesson_id
            session.stage = (
                SessionStage.COMPLETE if result.next_lesson_id is None else SessionStage.SYLLABUS
            )
        result.stage = session.stage
        self._store.save(session)
        _logger.info(
            "course %s: build review lesson %s -> %s",
            session.id, lesson.id, "passed" if passed else "keep going",
        )
        return result

    def get_hint(
        self,
        session_id: str,
        lesson_id: str | None,
        files_summary: str,
        grade: int | None = None,
    ) -> str:
        """Return one short hint for the learner's current build."""
        session = self._store.get(session_id)
        lesson = self._resolve_lesson(session, lesson_id)
        grade = _apply_grade(session, grade)
        data = self._ask_json(
            hint_prompt(session.mission, lesson, files_summary or "(no files yet)", grade)
        )
        return _plain_text(str(data.get("hint", "")).strip()) or "Try breaking the task into one small step."

    # --- Read ----------------------------------------------------------------

    def get_session(self, session_id: str) -> Session:
        """Return the stored session (for the browser to render or resume)."""
        return self._store.get(session_id)

    def list_sessions(self) -> list[Session]:
        """Return every stored session, newest first."""
        return self._store.list_sessions()

    def delete_session(self, session_id: str) -> None:
        """Delete a session so it no longer resumes or shows in the picker."""
        self._store.delete(session_id)

    # --- Internals -----------------------------------------------------------

    def _ask_json(self, prompt: str) -> dict:
        agent = self._agent or get_agent(
            get_settings().orchestrator_agent or get_settings().default_agent
        )
        answer = agent.complete(prompt, create_ephemeral())
        return extract_json(answer)

    def _resolve_lesson(self, session: Session, lesson_id: str | None) -> Lesson:
        target = lesson_id or session.active_lesson_id
        if target is None:
            raise OrchestratorError("no lesson specified and none is active")
        lesson = session.lesson(target)
        if lesson is None:
            raise OrchestratorError(f"session {session.id!r} has no lesson {target!r}")
        return lesson

    @staticmethod
    def _unlock_next(session: Session, lesson: Lesson) -> str | None:
        ids = [les.id for les in session.lessons]
        idx = ids.index(lesson.id)
        if idx + 1 < len(session.lessons):
            nxt = session.lessons[idx + 1]
            nxt.status = ProjectStatus.ACTIVE
            return nxt.id
        return None


# --- Module-level helpers (pure) ---------------------------------------------


def _clean(text: str, cap: int, label: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        raise OrchestratorError(f"{label} must not be empty")
    if len(cleaned) > cap:
        raise OrchestratorError(f"{label} is too long (max {cap} characters)")
    return cleaned


def _string_list(value: object, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise OrchestratorError(f"expected a list for {label}, got {type(value).__name__}")
    return [str(item).strip() for item in value if str(item).strip()]


def _parse_lessons(value: object) -> list[Lesson]:
    if not isinstance(value, list) or not value:
        raise OrchestratorError("the engine returned no lessons")
    lessons: list[Lesson] = []
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise OrchestratorError(f"lesson {index} is not an object")
        domain = str(raw.get("domain_type", "code")).strip().lower()
        lessons.append(
            Lesson(
                id=str(raw.get("id") or f"u{index}").strip(),
                title=_plain_text(str(raw.get("title", "")).strip()) or f"Lesson {index}",
                goal=_plain_text(str(raw.get("goal", "")).strip()),
                domain_type="interactive" if domain == "interactive" else "code",
                demo_task=_plain_text(str(raw.get("demo_task", "")).strip()),
                build_task=_plain_text(str(raw.get("build_task", "")).strip()),
                status=ProjectStatus.LOCKED,
            )
        )
    seen: set[str] = set()
    for index, les in enumerate(lessons, start=1):
        if les.id in seen:
            les.id = f"u{index}"
        seen.add(les.id)
    return lessons


def _parse_widget(value: object) -> Widget | None:
    if not isinstance(value, dict):
        return None
    return Widget(
        title=_plain_text(str(value.get("title", "")).strip()),
        caption=_plain_text(str(value.get("caption", "")).strip()),
        html=str(value.get("html", "")),  # HTML is code, not prose: do not sanitize
    )


def _parse_check(value: object) -> Check | None:
    if not isinstance(value, dict):
        return None
    kind = str(value.get("kind", "short")).strip().lower()
    return Check(
        question=_plain_text(str(value.get("question", "")).strip()),
        kind="mcq" if kind == "mcq" else "short",
        options=_plain_list(_string_list(value.get("options"), "options")),
    )


def _zip_qa(questions: list[str], answers: list[str]) -> list[tuple[str, str]]:
    return [
        (q, answers[i] if i < len(answers) else "") for i, q in enumerate(questions)
    ]


def _require_stage(session: Session, expected: SessionStage) -> None:
    if session.stage is not expected:
        raise OrchestratorError(
            f"session {session.id!r} is at stage {session.stage.value!r}, "
            f"expected {expected.value!r}"
        )


def _clamp_grade(grade: int) -> int:
    try:
        return max(_MIN_GRADE, min(_MAX_GRADE, int(grade)))
    except (TypeError, ValueError):
        return get_settings().reading_grade_default


def _apply_grade(session: Session, grade: int | None) -> int:
    if grade is not None:
        session.reading_grade = _clamp_grade(grade)
    return session.reading_grade


def _plain_text(text: str) -> str:
    return (
        text.replace(" — ", ", ")
        .replace(" – ", ", ")
        .replace("—", ", ")
        .replace("–", "-")
    )


def _plain_list(items: list[str]) -> list[str]:
    return [_plain_text(item) for item in items]


def _today() -> date:
    return datetime.now(timezone.utc).date()
