"""Server-Sent Events (SSE) wire formatting — the one place that knows the format.

The stream endpoint pushes agent activity to the browser over SSE (a plain
``text/event-stream`` HTTP response the browser consumes with ``EventSource``).
The exact byte format of a frame — ``data:`` lines terminated by a blank line —
lives only here so no route or bridge re-implements it (DRY).

Every frame this module emits carries a single ``data:`` line holding a JSON
object, so the browser can ``JSON.parse`` it and switch on one field. We do not
use named SSE events (the ``event:`` field): keeping every frame the default
event lets one client handler receive them all, and the payload's own ``kind``
says what it is.
"""

from __future__ import annotations

import json

from forgelearn.common.types import AgentEvent, EventKind

# The MIME type an SSE endpoint must advertise; browsers only treat a response
# as an event stream when it is served with exactly this media type.
SSE_MEDIA_TYPE = "text/event-stream"

# Response headers that keep an SSE stream flowing: disable caching and any
# proxy buffering so frames reach the browser the moment they are produced.
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # tell nginx-style proxies not to buffer the stream
}


def sse_frame(payload: dict) -> str:
    """Format one JSON payload as a single default-event SSE frame.

    Args:
        payload: A JSON-serializable object to place in the frame's ``data``.

    Returns:
        The frame text: one ``data:`` line plus the blank-line terminator.
    """
    # default=str keeps a stray non-JSON value (e.g. a datetime in ``data``)
    # from aborting the whole stream. json.dumps emits no raw newlines, so the
    # payload always fits on the single ``data:`` line SSE requires.
    return f"data: {json.dumps(payload, default=str)}\n\n"


def event_frame(event: AgentEvent) -> str:
    """Serialize a semantic :class:`AgentEvent` into an SSE frame.

    Args:
        event: The normalized agent event to send to the browser.

    Returns:
        An SSE frame whose ``data`` is the event as a JSON object (its ``kind``
        field tells the client how to render it).
    """
    return sse_frame(event.model_dump(mode="json"))


def error_frame(message: str) -> str:
    """Build a terminal error frame shaped like an ``ERROR`` :class:`AgentEvent`.

    Used when the agent run fails before (or instead of) emitting its own
    terminal event, so the browser always receives a well-formed error it can
    render and then close the stream on.

    Args:
        message: Human-readable description of what went wrong.

    Returns:
        An SSE frame carrying an error-kind payload with ``is_error`` set.
    """
    return event_frame(
        AgentEvent(kind=EventKind.ERROR, text=message, is_error=True)
    )
