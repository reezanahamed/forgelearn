"""Normalize a provider's raw event stream into clean, typed events (Phase 2).

Phase 1 adapters yield :class:`~forgelearn.agents.base.RawEvent` objects — the
provider's own JSON, lightly wrapped. This module turns that noisy, nested
stream into an ordered sequence of :class:`~forgelearn.common.types.AgentEvent`
objects the browser and orchestrator can render and reason about without
knowing which CLI ran underneath (BUILD_PHASES Phase 2).

The mapping understands the Claude Code / Anthropic ``stream-json`` schema
(verified against the headless docs, https://code.claude.com/docs/en/headless):

* ``system`` / ``init`` — session setup (model, tools) → one ``SYSTEM`` event.
* ``assistant`` — ``message.content`` is a list of blocks; each ``text`` block
  becomes ``NARRATION`` and each ``tool_use`` block becomes ``FILE_WRITE``,
  ``COMMAND``, or ``TOOL`` depending on the tool name. One assistant message can
  therefore fan out into several events.
* ``user`` — ``message.content`` carries ``tool_result`` blocks → ``TOOL_RESULT``.
* ``result`` — the terminal event → ``DONE`` (or ``ERROR`` if ``is_error``).

The mapping is deliberately defensive: every field is fetched with ``.get`` and
type-checked, so a partial, reordered, or unexpected line is skipped rather than
crashing the stream (robustness to partial/streamed output).
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from forgelearn.agents.base import RawEvent
from forgelearn.common.logging import get_logger
from forgelearn.common.types import AgentEvent, EventKind

_logger = get_logger("agents.events")

# --- Claude/Anthropic stream-json vocabulary (provider protocol, not settings) -
# These mirror the fixed protocol constants in ``claude.py``: they name the
# exact JSON shapes we normalize, so a schema change is a one-file edit here.
_SYSTEM_TYPE = "system"
_ASSISTANT_TYPE = "assistant"
_USER_TYPE = "user"
_RESULT_TYPE = "result"

_INIT_SUBTYPE = "init"
_API_RETRY_SUBTYPE = "api_retry"

_TEXT_BLOCK = "text"
_TOOL_USE_BLOCK = "tool_use"
_TOOL_RESULT_BLOCK = "tool_result"

# Tool names, grouped by what the learner sees them do. Anything not listed is a
# generic ``TOOL`` action (Read, Grep, Glob, WebFetch, TodoWrite, …).
_FILE_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit", "Update"})
_COMMAND_TOOL = "Bash"

# Where each tool stashes its target, tried in order for a display path.
_PATH_KEYS = ("file_path", "notebook_path", "path")
# Read-ish tools whose single argument is best shown as their target.
_TARGET_KEYS = ("pattern", "url", "query", "command", "prompt")

_MAX_RESULT_CHARS = 500  # keep tool-output lines printable, not a wall of text


def stream_events(raw_events: Iterable[RawEvent]) -> Iterator[AgentEvent]:
    """Normalize a stream of :class:`RawEvent` into semantic :class:`AgentEvent`.

    This is the lazy, streaming entry point: it consumes ``raw_events`` one at a
    time and yields events as they resolve, so a caller can print them live
    rather than waiting for the run to finish.

    Args:
        raw_events: The provider's raw event stream (e.g. ``adapter.run(...)``).

    Yields:
        :class:`AgentEvent` objects in the order they occur; one raw event may
        yield zero, one, or several.
    """
    for raw in raw_events:
        yield from to_events(raw)


def to_events(raw: RawEvent) -> list[AgentEvent]:
    """Map a single :class:`RawEvent` to zero or more :class:`AgentEvent`.

    Args:
        raw: One wrapped provider event.

    Returns:
        The semantic events it represents. Empty when the event carries nothing
        worth showing (e.g. an unrecognized type or an empty assistant message).
    """
    if raw.type == _ASSISTANT_TYPE:
        return _assistant_events(raw)
    if raw.type == _USER_TYPE:
        return _user_events(raw)
    if raw.type == _RESULT_TYPE:
        return [_result_event(raw)]
    if raw.type == _SYSTEM_TYPE:
        event = _system_event(raw)
        return [event] if event is not None else []
    _logger.debug("no semantic mapping for raw event type %r; skipping", raw.type)
    return []


def _session_id(raw: RawEvent) -> str | None:
    """Best-effort session id carried on every stream-json event."""
    value = raw.data.get("session_id")
    return value if isinstance(value, str) else None


def _content_blocks(raw: RawEvent) -> list[dict]:
    """Extract ``message.content`` as a list of block dicts, defensively."""
    message = raw.data.get("message")
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _system_event(raw: RawEvent) -> AgentEvent | None:
    """Turn a ``system`` event into a ``SYSTEM`` event, or ``None`` to skip."""
    session_id = _session_id(raw)
    if raw.subtype == _INIT_SUBTYPE:
        model = raw.data.get("model")
        text = f"session started (model {model})" if model else "session started"
        return AgentEvent(
            kind=EventKind.SYSTEM, text=text, session_id=session_id, data=raw.data
        )
    if raw.subtype == _API_RETRY_SUBTYPE:
        attempt = raw.data.get("attempt")
        max_retries = raw.data.get("max_retries")
        return AgentEvent(
            kind=EventKind.SYSTEM,
            text=f"retrying API request (attempt {attempt}/{max_retries})",
            session_id=session_id,
            data=raw.data,
        )
    # Other system subtypes (plugin_install, …) aren't learner-facing; drop them
    # but keep the payload discoverable via debug logs.
    _logger.debug("skipping system event subtype %r", raw.subtype)
    return None


def _assistant_events(raw: RawEvent) -> list[AgentEvent]:
    """Fan an assistant message out into narration and tool-use events."""
    session_id = _session_id(raw)
    events: list[AgentEvent] = []
    for block in _content_blocks(raw):
        block_type = block.get("type")
        if block_type == _TEXT_BLOCK:
            text = str(block.get("text", "")).strip()
            if text:
                events.append(
                    AgentEvent(
                        kind=EventKind.NARRATION, text=text, session_id=session_id
                    )
                )
        elif block_type == _TOOL_USE_BLOCK:
            events.append(_tool_use_event(block, session_id))
        # thinking / redacted_thinking / unknown blocks are internal — skip.
    return events


def _tool_use_event(block: dict, session_id: str | None) -> AgentEvent:
    """Classify one ``tool_use`` block into FILE_WRITE / COMMAND / TOOL."""
    name = str(block.get("name", "tool"))
    tool_input = block.get("input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    path = _first_str(tool_input, _PATH_KEYS)

    if name in _FILE_WRITE_TOOLS:
        text = f"{name} {path}" if path else name
        return AgentEvent(
            kind=EventKind.FILE_WRITE,
            text=text,
            tool=name,
            path=path,
            session_id=session_id,
            data=dict(tool_input),
        )
    if name == _COMMAND_TOOL:
        command = _first_str(tool_input, ("command",)) or ""
        return AgentEvent(
            kind=EventKind.COMMAND,
            text=command or name,
            tool=name,
            session_id=session_id,
            data=dict(tool_input),
        )
    target = path or _first_str(tool_input, _TARGET_KEYS)
    return AgentEvent(
        kind=EventKind.TOOL,
        text=f"{name} {target}" if target else name,
        tool=name,
        path=path,
        session_id=session_id,
        data=dict(tool_input),
    )


def _user_events(raw: RawEvent) -> list[AgentEvent]:
    """Turn tool-result blocks in a user message into TOOL_RESULT events."""
    session_id = _session_id(raw)
    events: list[AgentEvent] = []
    for block in _content_blocks(raw):
        if block.get("type") != _TOOL_RESULT_BLOCK:
            continue
        text = _flatten_result_content(block.get("content"))
        events.append(
            AgentEvent(
                kind=EventKind.TOOL_RESULT,
                text=text,
                is_error=bool(block.get("is_error", False)),
                session_id=session_id,
                data={"tool_use_id": block.get("tool_use_id")},
            )
        )
    return events


def _result_event(raw: RawEvent) -> AgentEvent:
    """Turn the terminal ``result`` event into DONE (or ERROR)."""
    is_error = bool(raw.data.get("is_error", False))
    text = str(raw.data.get("result", "")).strip()
    if not text:
        text = "run failed" if is_error else "run complete"
    return AgentEvent(
        kind=EventKind.ERROR if is_error else EventKind.DONE,
        text=text,
        is_error=is_error,
        session_id=_session_id(raw),
        data=raw.data,
    )


def _first_str(source: dict, keys: Iterable[str]) -> str | None:
    """Return the first non-empty string value among ``keys`` in ``source``."""
    for key in keys:
        value = source.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _flatten_result_content(content: object) -> str:
    """Flatten a tool_result ``content`` (string or block list) to plain text.

    Anthropic tool results carry either a bare string or a list of content
    blocks (each usually ``{"type": "text", "text": ...}``). We join the text
    and truncate so a large file dump stays a single printable line.
    """
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == _TEXT_BLOCK
        ]
        text = "\n".join(part for part in parts if part)
    else:
        text = ""
    text = text.strip()
    if len(text) > _MAX_RESULT_CHARS:
        text = text[:_MAX_RESULT_CHARS] + "…"
    return text
