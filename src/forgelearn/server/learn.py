"""The learning-method API — the orchestrator over HTTP (Phase 7).

These routes expose the state machine in :mod:`forgelearn.orchestrator` to the
browser: start a session and get interview questions, submit answers to receive
a mission + ladder, build the active rung (a live agent stream), and submit a
teach-back to unlock the next rung. They are kept in their own router (included
by :mod:`forgelearn.server.app`) so the Phase 3–6 transport routes stay focused.

The step endpoints are ``POST`` with JSON bodies (they change server state);
reading a session and building a rung are ``GET`` — building is SSE, which the
browser's ``EventSource`` can only issue as a GET. Every orchestrator failure is
a typed :class:`~forgelearn.common.errors.ForgeLearnError`, translated here into a
clean ``400`` with a message instead of a 500.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from forgelearn.common.errors import ForgeLearnError
from forgelearn.common.logging import get_logger
from forgelearn.common.types import Session
from forgelearn.config import get_settings
from forgelearn.orchestrator import Orchestrator
from forgelearn.server.sse import SSE_HEADERS, SSE_MEDIA_TYPE
from forgelearn.server.streams import stream_build_sse
from forgelearn.storage import export_session_html

_logger = get_logger("server.learn")

# Route paths as named constants so the frontend contract lives in one place.
LEARN_START_PATH = "/api/learn/start"
LEARN_INTERVIEW_PATH = "/api/learn/interview"
LEARN_SESSION_PATH = "/api/learn/session"
LEARN_SESSIONS_PATH = "/api/learn/sessions"
LEARN_BUILD_PATH = "/api/learn/build"
LEARN_TEACHBACK_PATH = "/api/learn/teachback"
LEARN_EXPORT_PATH = "/api/learn/export"

router = APIRouter()


# --- Request bodies ----------------------------------------------------------


class StartRequest(BaseModel):
    """Body for :func:`start` — the subject the learner wants to learn."""

    topic: str = Field(..., min_length=1, description="What the learner wants to learn.")


class InterviewRequest(BaseModel):
    """Body for :func:`interview` — the learner's answers to the questions."""

    session: str = Field(..., min_length=1, description="The session being answered.")
    answers: list[str] = Field(default_factory=list, description="Answers, in order.")


class TeachBackRequest(BaseModel):
    """Body for :func:`teachback` — the learner's own-words explanation."""

    session: str = Field(..., min_length=1, description="The session being assessed.")
    project: str | None = Field(None, description="Rung id; defaults to the current one.")
    explanation: str = Field(..., min_length=1, description="The learner's explanation.")


# --- Helpers -----------------------------------------------------------------


def _session_payload(session: Session) -> dict:
    """Serialize a session to JSON the browser renders the whole flow from."""
    return session.model_dump(mode="json")


def _error(exc: ForgeLearnError) -> JSONResponse:
    """Translate a typed orchestrator failure into a 400 with its message."""
    _logger.warning("learn request failed: %s", exc)
    return JSONResponse({"error": str(exc)}, status_code=400)


# --- Routes ------------------------------------------------------------------


@router.post(LEARN_START_PATH)
def start(body: StartRequest) -> JSONResponse:
    """Start a learning session and return its interview questions.

    Args:
        body: The learner's topic.

    Returns:
        The new session (stage ``interview``) as JSON, or a 400 on failure.
    """
    try:
        session = Orchestrator().start(body.topic)
    except ForgeLearnError as exc:
        return _error(exc)
    return JSONResponse(_session_payload(session))


@router.post(LEARN_INTERVIEW_PATH)
def interview(body: InterviewRequest) -> JSONResponse:
    """Submit interview answers; return the generated mission + ladder.

    Args:
        body: The session id and the learner's answers.

    Returns:
        The session (stage ``ladder``, mission + projects set) as JSON, or a 400.
    """
    try:
        session = Orchestrator().submit_interview(body.session, body.answers)
    except ForgeLearnError as exc:
        return _error(exc)
    return JSONResponse(_session_payload(session))


@router.get(LEARN_SESSION_PATH)
def session(
    session: str = Query(..., min_length=1, description="The session to fetch."),
) -> JSONResponse:
    """Return a session's full state (for the browser to render or resume).

    Args:
        session: The session id to fetch.

    Returns:
        The session as JSON, or a 400 if it is unknown.
    """
    try:
        found = Orchestrator().get_session(session)
    except ForgeLearnError as exc:
        return _error(exc)
    return JSONResponse(_session_payload(found))


@router.get(LEARN_SESSIONS_PATH)
def sessions() -> JSONResponse:
    """List saved sessions (newest first) so a returning learner can resume.

    Returns:
        A JSON object ``{"sessions": [{"id", "topic", "mission", "stage",
        "created_at"}, ...]}`` — a lightweight index, not the full documents.
    """
    saved = Orchestrator().list_sessions()
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


@router.get(LEARN_BUILD_PATH)
def build(
    session: str = Query(..., min_length=1, description="The session whose rung to build."),
    project: str | None = Query(None, description="Rung id; defaults to the current one."),
    agent: str | None = Query(None, description="Provider to build with; else the default."),
) -> StreamingResponse:
    """Build the session's active rung and stream the agent's activity as SSE.

    Args:
        session: The learning session whose rung to build.
        project: Optional rung id; defaults to the current active rung.
        agent: Optional provider override; falls back to the configured default.

    Returns:
        A ``text/event-stream`` of the build's normalized events. A locked rung or
        unknown session surfaces as a terminal error frame, not a dropped stream.
    """
    agent_name = agent or get_settings().default_agent
    _logger.info("build request: session=%r project=%r agent=%r", session, project, agent_name)
    return StreamingResponse(
        stream_build_sse(session, project, agent_name),
        media_type=SSE_MEDIA_TYPE,
        headers=SSE_HEADERS,
    )


@router.post(LEARN_TEACHBACK_PATH)
def teachback(body: TeachBackRequest) -> JSONResponse:
    """Judge a teach-back; on a pass, unlock the next rung and log progress.

    Args:
        body: The session, the rung, and the learner's explanation.

    Returns:
        JSON with the verdict (``passed``, ``probes``, ``feedback``, notes, the
        unlocked ``next_project_id``) plus the refreshed ``session``; or a 400.
    """
    try:
        orchestrator = Orchestrator()
        result = orchestrator.submit_teachback(body.session, body.project, body.explanation)
        session = orchestrator.get_session(body.session)
    except ForgeLearnError as exc:
        return _error(exc)
    return JSONResponse(
        {
            "passed": result.passed,
            "probes": result.teachback.probes,
            "feedback": result.teachback.feedback,
            "progress_note": result.progress_note,
            "storage_note": result.storage_note,
            "next_project_id": result.next_project_id,
            "stage": result.stage.value,
            "session": _session_payload(session),
        }
    )


@router.get(LEARN_EXPORT_PATH)
def export(
    session: str = Query(..., min_length=1, description="The session to export."),
) -> Response:
    """Export a session as a self-contained, downloadable HTML file.

    The document inlines the mission, ladder, progress, teach-backs, and every
    file the agent built (assets as ``data:`` URIs), so it works offline (PLAN §8a).

    Args:
        session: The session id to export.

    Returns:
        A ``text/html`` response marked as a download, or a 400 if the session is
        unknown.
    """
    try:
        found = Orchestrator().get_session(session)
    except ForgeLearnError as exc:
        return _error(exc)
    html = export_session_html(found)
    filename = f"forgelearn-{session}.html"
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
