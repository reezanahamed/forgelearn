"""HTTP routes for the ForgeLearn server (Phase 3, extended in Phase 5).

Concerns, one router:

* ``GET /`` serves the browser UI (the static ``index.html``).
* ``GET /api/health`` and ``GET /api/agents`` are small JSON status endpoints.
* ``GET /api/stream`` runs the agent and streams its activity (SSE).
* ``GET /api/files`` / ``GET /api/file`` expose a session workspace's file tree
  and a single file's contents (Phase 5).
* ``GET /api/run`` executes the session's built project and streams its output
  (SSE, Phase 5).

The two streaming endpoints are **GET** with query parameters because the
browser's ``EventSource`` client can only issue GETs; each returns a
``StreamingResponse`` wrapping a synchronous frame generator from
:mod:`forgelearn.server.streams`.
"""

from __future__ import annotations

import mimetypes

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from forgelearn import __version__
from forgelearn.agents import available_agents
from forgelearn.common.errors import WorkspaceError
from forgelearn.common.logging import get_logger
from forgelearn.config import get_settings
from forgelearn.server.sse import SSE_HEADERS, SSE_MEDIA_TYPE
from forgelearn.server.streams import stream_agent_sse, stream_run_sse
from forgelearn.workspace import list_files, read_bytes, read_file

_logger = get_logger("server.routes")

# Route paths kept as named constants so the frontend contract lives in one
# place and there are no bare path strings scattered through the handlers.
INDEX_PATH = "/"
HEALTH_PATH = "/api/health"
AGENTS_PATH = "/api/agents"
STREAM_PATH = "/api/stream"
FILES_PATH = "/api/files"
FILE_PATH = "/api/file"
FILE_RAW_PATH = "/api/file/raw"
RUN_PATH = "/api/run"

_INDEX_FILENAME = "index.html"

router = APIRouter()


@router.get(HEALTH_PATH)
def health() -> JSONResponse:
    """Report liveness plus basic server/agent info.

    Returns:
        A JSON object with the app version, the default agent, and the list of
        registered agent providers.
    """
    settings = get_settings()
    return JSONResponse(
        {
            "status": "ok",
            "version": __version__,
            "default_agent": settings.default_agent,
            "agents": available_agents(),
        }
    )


@router.get(AGENTS_PATH)
def agents() -> JSONResponse:
    """List the agent providers the browser dropdown can choose (Phase 6 seam).

    Returns:
        A JSON object with the available provider names and the current default.
    """
    settings = get_settings()
    return JSONResponse(
        {"agents": available_agents(), "default_agent": settings.default_agent}
    )


@router.get(STREAM_PATH)
def stream(
    prompt: str = Query(..., min_length=1, description="What to ask the agent to build."),
    agent: str | None = Query(None, description="Provider to run; defaults to config."),
    session: str | None = Query(None, description="Session whose workspace the files land in."),
) -> StreamingResponse:
    """Run the agent on ``prompt`` and stream its events to the browser as SSE.

    Args:
        prompt: The natural-language instruction for the agent (required).
        agent: Optional provider override; falls back to the configured default.
        session: Optional browser session id; its workspace persists the files
            the agent writes so they can later be listed and run.

    Returns:
        A ``text/event-stream`` response whose frames are the agent's normalized
        events, ending when the run completes or fails.
    """
    agent_name = agent or get_settings().default_agent
    _logger.info("stream request: agent=%r session=%r", agent_name, session)
    return StreamingResponse(
        stream_agent_sse(prompt, agent_name, session),
        media_type=SSE_MEDIA_TYPE,
        headers=SSE_HEADERS,
    )


@router.get(FILES_PATH)
def files(
    session: str = Query(..., min_length=1, description="Session whose workspace to list."),
) -> JSONResponse:
    """List the files in a session's workspace for the browser file tree.

    Args:
        session: The session whose workspace to list (required).

    Returns:
        A JSON object ``{"files": [{"path", "size"}, ...]}``. An invalid session
        id yields a 400; an absent/empty workspace yields an empty list.
    """
    try:
        entries = list_files(session)
    except WorkspaceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"files": [{"path": e.path, "size": e.size} for e in entries]})


@router.get(FILE_PATH)
def file(
    session: str = Query(..., min_length=1, description="Session that owns the file."),
    path: str = Query(..., min_length=1, description="Workspace-relative file path."),
) -> JSONResponse:
    """Return one workspace file's contents for the browser code viewer.

    Args:
        session: The session that owns the file (required).
        path: Workspace-relative path of the file to read (required).

    Returns:
        A JSON object ``{"path", "content"}``; a missing file, bad session, or a
        path escaping the workspace yields a 400.
    """
    try:
        content = read_file(session, path)
    except WorkspaceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"path": path, "content": content})


@router.get(FILE_RAW_PATH)
def file_raw(
    session: str = Query(..., min_length=1, description="Session that owns the file."),
    path: str = Query(..., min_length=1, description="Workspace-relative file path."),
) -> Response:
    """Serve a workspace file's raw bytes so the browser can render it directly.

    Used by the viewer to show images (a PNG plot, an SVG) as pictures instead of
    garbled text. The content type is guessed from the extension. Path handling and
    traversal defence are the same as the text endpoint (in the workspace layer).

    Args:
        session: The session that owns the file (required).
        path: Workspace-relative path of the file to read (required).

    Returns:
        The raw file bytes with a guessed media type; a missing file, bad session,
        or a path escaping the workspace yields a 400.
    """
    try:
        data = read_bytes(session, path)
    except WorkspaceError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    media_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return Response(content=data, media_type=media_type)


@router.get(RUN_PATH)
def run(
    session: str = Query(..., min_length=1, description="Session whose project to run."),
) -> StreamingResponse:
    """Execute the session's built project and stream its output as SSE.

    Args:
        session: The session whose workspace project to run (required).

    Returns:
        A ``text/event-stream`` response: a command frame, one frame per output
        line, then a terminal done/error frame.
    """
    _logger.info("run request: session=%r", session)
    return StreamingResponse(
        stream_run_sse(session),
        media_type=SSE_MEDIA_TYPE,
        headers=SSE_HEADERS,
    )


@router.get(INDEX_PATH, include_in_schema=False)
def index() -> FileResponse:
    """Serve the browser UI (the static ``index.html``).

    Returns:
        The ``index.html`` file from the configured frontend directory.
    """
    return FileResponse(get_settings().frontend_dir / _INDEX_FILENAME)
