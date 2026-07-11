"""Bridge the engine (agent + Run) to browser SSE streams (Phase 3, 5).

Two live sources feed the browser, and both are wrapped the same way here:

* :func:`stream_agent_sse` runs a headless CLI agent on a prompt (Phase 3), and
* :func:`stream_run_sse` executes a session's built project (Phase 5).

Each produces normalized :class:`~forgelearn.common.types.AgentEvent` objects from
a **synchronous, blocking** generator (subprocess I/O read line by line);
Starlette drains a sync generator in a worker thread, so the event loop stays
free. :func:`_sse_from_events` is the shared wrapper: it turns events into SSE
frames and converts any engine failure into a terminal error frame, so the
browser always gets a clean end-of-stream instead of a dropped connection.
"""

from __future__ import annotations

from collections.abc import Iterator

from forgelearn.agents import get_agent
from forgelearn.common.errors import ForgeLearnError
from forgelearn.common.logging import get_logger
from forgelearn.common.types import AgentEvent, EventKind
from forgelearn.orchestrator import Orchestrator
from forgelearn.server.sse import error_frame, event_frame
from forgelearn.workspace import create_ephemeral, get_or_create, run_workspace

_logger = get_logger("server.streams")


def _sse_from_events(events: Iterator[AgentEvent], *, context: str) -> Iterator[str]:
    """Wrap an event generator as SSE frames, framing any failure as an error.

    Args:
        events: A (lazy) generator of normalized agent/run events.
        context: Short description used in logs (e.g. ``"agent stream"``).

    Yields:
        SSE frame strings, ending with a terminal error frame if the underlying
        generator raises instead of completing.
    """
    try:
        for event in events:
            yield event_frame(event)
    except ForgeLearnError as exc:
        # Expected, typed failures (unknown agent, missing CLI, timeout, no
        # runnable file, bad session). Log at the boundary, tell the browser.
        _logger.warning("%s failed: %s", context, exc)
        yield error_frame(str(exc))
    except Exception as exc:  # noqa: BLE001 — last-resort guard for the stream
        # Never let an unexpected error drop the connection silently.
        _logger.exception("unexpected error in %s", context)
        yield error_frame(f"unexpected server error: {exc}")


def _agent_events(prompt: str, agent_name: str, session_id: str | None) -> Iterator[AgentEvent]:
    """Resolve the workspace and run the agent, yielding its events."""
    workspace = get_or_create(session_id) if session_id else create_ephemeral()
    agent = get_agent(agent_name)
    yield from agent.run_events(prompt, workspace)


def stream_agent_sse(
    prompt: str, agent_name: str, session_id: str | None = None
) -> Iterator[str]:
    """Run the agent on ``prompt`` and yield its activity as SSE frames.

    Args:
        prompt: The natural-language instruction for the agent.
        agent_name: Registered provider to run (e.g. ``"claude"``).
        session_id: Browser session whose workspace the files land in; when
            ``None`` a throwaway workspace is used (files aren't tracked).

    Yields:
        SSE frame strings, in order, ending once the agent run completes or fails.
    """
    _logger.info(
        "starting agent stream: agent=%r session=%r prompt=%r",
        agent_name,
        session_id,
        prompt[:120],
    )
    return _sse_from_events(
        _agent_events(prompt, agent_name, session_id),
        context=f"agent stream (agent={agent_name!r})",
    )


def _build_events(
    session_id: str, project_id: str | None, agent_name: str
) -> Iterator[AgentEvent]:
    """Build the orchestrator's active rung, streaming the agent's activity.

    The orchestrator (Phase 7) composes the grounded, teaching build prompt and
    tracks state; the browser-chosen provider does the actual building into the
    session's own workspace so the files persist for listing and Run. When the
    build finishes cleanly the rung is marked built, which opens its teach-back
    gate.
    """
    orchestrator = Orchestrator()
    prompt = orchestrator.build_instruction(session_id, project_id)
    workspace = get_or_create(session_id)
    agent = get_agent(agent_name)

    succeeded = False
    for event in agent.run_events(prompt, workspace):
        if event.kind is EventKind.DONE:
            succeeded = True
        yield event
    if succeeded:
        # Only open the teach-back gate on a clean build; a failed/aborted build
        # leaves the rung un-built so the learner can retry.
        orchestrator.mark_built(session_id, project_id)


def stream_build_sse(
    session_id: str, project_id: str | None, agent_name: str
) -> Iterator[str]:
    """Build a ladder rung for a learning session and yield SSE frames.

    Args:
        session_id: The learning session whose active rung to build.
        project_id: The rung to build; ``None`` uses the session's current rung.
        agent_name: Registered provider to build with (from the dropdown).

    Yields:
        SSE frame strings, ending once the build completes or fails.
    """
    _logger.info(
        "starting build stream: session=%r project=%r agent=%r",
        session_id,
        project_id,
        agent_name,
    )
    return _sse_from_events(
        _build_events(session_id, project_id, agent_name),
        context=f"build (session={session_id!r})",
    )


def stream_run_sse(session_id: str) -> Iterator[str]:
    """Run the session's built project and yield its output as SSE frames.

    Args:
        session_id: The session whose workspace project to execute.

    Yields:
        SSE frame strings: the command, one per output line, then a terminal
        done/error frame.
    """
    _logger.info("starting run stream: session=%r", session_id)
    return _sse_from_events(
        run_workspace(session_id), context=f"run (session={session_id!r})"
    )
