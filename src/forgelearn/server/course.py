"""The guided-lesson API, the redesign's orchestrator over HTTP (Phase B).

Exposes :class:`~forgelearn.orchestrator.course_engine.CourseOrchestrator` to the
browser: start a session, submit the interview to get a syllabus, open a lesson
(teach), answer its check, watch the AI demo (a live stream), build your own
version, and ask for hints. It mirrors the transport conventions of the older
learn API (POST for state changes, GET+SSE for the streamed demo) so the frontend
contract is consistent.

The build-review and hint endpoints read the learner's workspace files here and
pass them to the engine as a text summary, keeping the engine pure of I/O.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from forgelearn.common.errors import ForgeLearnError
from forgelearn.common.logging import get_logger
from forgelearn.common.types import Session
from forgelearn.config import get_settings
from forgelearn.orchestrator.course_engine import CourseOrchestrator
from forgelearn.server.sse import SSE_HEADERS, SSE_MEDIA_TYPE
from forgelearn.server.streams import stream_demo_sse
from forgelearn.workspace import workspace_summary

_logger = get_logger("server.course")

COURSE_START_PATH = "/api/course/start"
COURSE_INTERVIEW_PATH = "/api/course/interview"
COURSE_SESSION_PATH = "/api/course/session"
COURSE_SESSIONS_PATH = "/api/course/sessions"
COURSE_OPEN_PATH = "/api/course/open"
COURSE_CHECK_PATH = "/api/course/check"
COURSE_DEMO_PATH = "/api/course/demo"
COURSE_BUILD_PATH = "/api/course/build"
COURSE_HINT_PATH = "/api/course/hint"

router = APIRouter()


# --- Request bodies ----------------------------------------------------------


class StartRequest(BaseModel):
    topic: str = Field(..., min_length=1, description="What the learner wants to learn.")
    grade: int | None = Field(None, description="School grade level to write for.")


class InterviewRequest(BaseModel):
    session: str = Field(..., min_length=1)
    answers: list[str] = Field(default_factory=list)
    grade: int | None = None


class LessonRequest(BaseModel):
    session: str = Field(..., min_length=1)
    lesson: str | None = Field(None, description="Lesson id; defaults to the active one.")
    grade: int | None = None


class CheckRequest(LessonRequest):
    answer: str = Field(..., min_length=1, description="The learner's check answer.")


# --- Helpers -----------------------------------------------------------------


def _payload(session: Session) -> dict:
    return session.model_dump(mode="json")


def _error(exc: ForgeLearnError) -> JSONResponse:
    _logger.warning("course request failed: %s", exc)
    return JSONResponse({"error": str(exc)}, status_code=400)


# --- Routes ------------------------------------------------------------------


@router.post(COURSE_START_PATH)
def start(body: StartRequest) -> JSONResponse:
    """Start a course and return its interview questions."""
    try:
        session = CourseOrchestrator().start(body.topic, body.grade)
    except ForgeLearnError as exc:
        return _error(exc)
    return JSONResponse(_payload(session))


@router.post(COURSE_INTERVIEW_PATH)
def interview(body: InterviewRequest) -> JSONResponse:
    """Submit interview answers; return the mission + syllabus of lessons."""
    try:
        session = CourseOrchestrator().submit_interview(body.session, body.answers, body.grade)
    except ForgeLearnError as exc:
        return _error(exc)
    return JSONResponse(_payload(session))


@router.get(COURSE_SESSION_PATH)
def session(session: str = Query(..., min_length=1)) -> JSONResponse:
    """Return a course session's full state (to render or resume)."""
    try:
        found = CourseOrchestrator().get_session(session)
    except ForgeLearnError as exc:
        return _error(exc)
    return JSONResponse(_payload(found))


@router.get(COURSE_SESSIONS_PATH)
def sessions() -> JSONResponse:
    """List saved sessions (newest first) for a resume picker."""
    saved = CourseOrchestrator().list_sessions()
    return JSONResponse(
        {
            "sessions": [
                {
                    "id": s.id,
                    "topic": s.topic,
                    "mission": s.mission,
                    "stage": s.stage.value,
                    "created_at": s.created_at.isoformat(),
                }
                for s in saved
            ]
        }
    )


@router.post(COURSE_OPEN_PATH)
def open_lesson(body: LessonRequest) -> JSONResponse:
    """Open a lesson: generate its concept + interactive widget + check."""
    try:
        session = CourseOrchestrator().open_lesson(body.session, body.lesson, body.grade)
    except ForgeLearnError as exc:
        return _error(exc)
    return JSONResponse(_payload(session))


@router.post(COURSE_CHECK_PATH)
def check(body: CheckRequest) -> JSONResponse:
    """Grade the lesson's understanding check and return the verdict."""
    try:
        orchestrator = CourseOrchestrator()
        result = orchestrator.submit_check(body.session, body.lesson, body.answer, body.grade)
        session = orchestrator.get_session(body.session)
    except ForgeLearnError as exc:
        return _error(exc)
    return JSONResponse(
        {
            "correct": result.correct,
            "feedback": result.feedback,
            "explanation": result.explanation,
            "stage": result.stage.value,
            "session": _payload(session),
        }
    )


@router.get(COURSE_DEMO_PATH)
def demo(
    session: str = Query(..., min_length=1),
    lesson: str | None = Query(None),
    agent: str | None = Query(None),
    grade: int | None = Query(None),
) -> StreamingResponse:
    """Build the lesson's worked example and stream the agent's activity as SSE."""
    agent_name = agent or get_settings().default_agent
    _logger.info("demo request: session=%r lesson=%r agent=%r", session, lesson, agent_name)
    return StreamingResponse(
        stream_demo_sse(session, lesson, agent_name, grade),
        media_type=SSE_MEDIA_TYPE,
        headers=SSE_HEADERS,
    )


@router.post(COURSE_BUILD_PATH)
def build(body: LessonRequest) -> JSONResponse:
    """Review the learner's own build (reads their workspace files) and coach."""
    try:
        orchestrator = CourseOrchestrator()
        summary = workspace_summary(body.session)
        result = orchestrator.submit_build(body.session, body.lesson, summary, body.grade)
        session = orchestrator.get_session(body.session)
    except ForgeLearnError as exc:
        return _error(exc)
    return JSONResponse(
        {
            "passed": result.passed,
            "feedback": result.feedback,
            "hints": result.hints,
            "progress_note": result.progress_note,
            "next_lesson_id": result.next_lesson_id,
            "stage": result.stage.value,
            "session": _payload(session),
        }
    )


@router.post(COURSE_HINT_PATH)
def hint(body: LessonRequest) -> JSONResponse:
    """Return one short hint on the learner's current build."""
    try:
        orchestrator = CourseOrchestrator()
        summary = workspace_summary(body.session)
        text = orchestrator.get_hint(body.session, body.lesson, summary, body.grade)
    except ForgeLearnError as exc:
        return _error(exc)
    return JSONResponse({"hint": text})
