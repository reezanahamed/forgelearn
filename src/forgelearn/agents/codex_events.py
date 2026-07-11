"""Normalize OpenAI Codex CLI output into ForgeLearn's semantic events (Phase 6).

The Codex CLI (``codex exec --json``) streams a *different* JSON schema from
Claude Code, so it gets its own normalizer that lands on the very same
:class:`~forgelearn.common.types.AgentEvent` vocabulary. That is the whole point
of the adapter layer: downstream code (server, browser, orchestrator) renders
one event shape and never learns which CLI produced it.

Codex emits newline-delimited JSON (verified against the official non-interactive
docs, https://developers.openai.com/codex/noninteractive, July 2026). Top-level
lines carry a ``type``:

* ``thread.started`` — run began (has ``thread_id``) → ``SYSTEM``.
* ``turn.started`` — internal turn boundary → dropped (nothing learner-facing).
* ``turn.completed`` — the run's terminal success event → ``DONE``.
* ``turn.failed`` / ``error`` — the run failed → ``ERROR``.
* ``item.started`` / ``item.updated`` / ``item.completed`` — a work item changed
  state; ``item.type`` decides the mapping:
    - ``agent_message`` → ``NARRATION`` (the teaching voice),
    - ``command_execution`` → ``COMMAND`` when it starts, ``TOOL_RESULT`` when it
      completes (carrying ``aggregated_output`` and ``exit_code``),
    - ``file_change`` → one ``FILE_WRITE`` per changed path,
    - ``mcp_tool_call`` → ``TOOL``,
    - ``reasoning`` / ``todo_list`` → ``SYSTEM`` (internal; hidden by the UI),
    - ``error`` → ``ERROR``.

Every field is fetched defensively with ``.get`` and type-checked, so a partial,
reordered, or unknown line is skipped rather than crashing the stream.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from forgelearn.agents.base import RawEvent
from forgelearn.common.logging import get_logger
from forgelearn.common.types import AgentEvent, EventKind

_logger = get_logger("agents.codex_events")

# --- Codex exec --json vocabulary (provider protocol, not settings) ----------
_THREAD_STARTED = "thread.started"
_TURN_STARTED = "turn.started"
_TURN_COMPLETED = "turn.completed"
_TURN_FAILED = "turn.failed"
_ERROR_TYPE = "error"
_ITEM_PREFIX = "item."
_ITEM_COMPLETED = "item.completed"
_ITEM_STARTED = "item.started"

# item.type values Codex uses for the objects inside an item.* line.
_AGENT_MESSAGE = "agent_message"
_REASONING = "reasoning"
_COMMAND_EXECUTION = "command_execution"
_FILE_CHANGE = "file_change"
_MCP_TOOL_CALL = "mcp_tool_call"
_TODO_LIST = "todo_list"
_ITEM_ERROR = "error"

_MAX_RESULT_CHARS = 500  # keep command output printable, not a wall of text


def stream_events(raw_events: Iterable[RawEvent]) -> Iterator[AgentEvent]:
    """Normalize a Codex :class:`RawEvent` stream into :class:`AgentEvent`.

    Lazy, streaming entry point (mirrors ``events.stream_events`` for Claude).
    As a robustness guarantee it always ends on a terminal event: if the Codex
    stream drains without a ``turn.completed``/``turn.failed``/``error`` (e.g. a
    schema change or a truncated run), a synthetic ``DONE`` is appended so the
    browser — which closes its SSE connection on the terminal event — never
    hangs waiting for one.

    Args:
        raw_events: The Codex adapter's raw event stream.

    Yields:
        :class:`AgentEvent` objects in order, ending with a terminal event.
    """
    saw_terminal = False
    for raw in raw_events:
        for event in to_events(raw):
            if event.kind in (EventKind.DONE, EventKind.ERROR):
                saw_terminal = True
            yield event
    if not saw_terminal:
        yield AgentEvent(kind=EventKind.DONE, text="run complete")


def to_events(raw: RawEvent) -> list[AgentEvent]:
    """Map a single Codex :class:`RawEvent` to zero or more :class:`AgentEvent`.

    Args:
        raw: One wrapped Codex event line.

    Returns:
        The semantic events it represents (possibly empty).
    """
    if raw.type.startswith(_ITEM_PREFIX):
        return _item_events(raw)
    if raw.type == _THREAD_STARTED:
        thread_id = raw.data.get("thread_id")
        thread_id = thread_id if isinstance(thread_id, str) else None
        return [
            AgentEvent(
                kind=EventKind.SYSTEM,
                text="session started",
                session_id=thread_id,
                data=raw.data,
            )
        ]
    if raw.type == _TURN_COMPLETED:
        return [AgentEvent(kind=EventKind.DONE, text="run complete", data=raw.data)]
    if raw.type == _TURN_FAILED:
        return [_failure_event(_nested_message(raw.data.get("error")), raw.data)]
    if raw.type == _ERROR_TYPE:
        return [_failure_event(_str(raw.data.get("message")), raw.data)]
    if raw.type == _TURN_STARTED:
        return []  # internal boundary, nothing learner-facing
    _logger.debug("no semantic mapping for Codex event type %r; skipping", raw.type)
    return []


def _item_events(raw: RawEvent) -> list[AgentEvent]:
    """Map an ``item.*`` line to events based on its ``item.type`` and phase."""
    item = raw.data.get("item")
    if not isinstance(item, dict):
        return []
    item_type = item.get("type")
    phase = raw.type  # item.started / item.updated / item.completed
    completed = phase == _ITEM_COMPLETED
    started = phase == _ITEM_STARTED

    if item_type == _AGENT_MESSAGE:
        # The final assistant text arrives once, on completion.
        text = _str(item.get("text")).strip()
        return [AgentEvent(kind=EventKind.NARRATION, text=text)] if completed and text else []

    if item_type == _COMMAND_EXECUTION:
        return _command_events(item, started=started, completed=completed)

    if item_type == _FILE_CHANGE:
        return _file_change_events(item) if completed else []

    if item_type == _MCP_TOOL_CALL:
        if not started:
            return []
        server = _str(item.get("server"))
        tool = _str(item.get("tool"))
        label = f"{server}:{tool}".strip(":") or "tool"
        return [AgentEvent(kind=EventKind.TOOL, text=label, tool=tool or None)]

    if item_type == _ITEM_ERROR:
        return [_failure_event(_str(item.get("message")), item)] if completed else []

    if item_type in (_REASONING, _TODO_LIST):
        # Internal thinking / task bookkeeping — kept as SYSTEM so the UI hides it.
        if not completed:
            return []
        return [AgentEvent(kind=EventKind.SYSTEM, text=item_type, data=item)]

    _logger.debug("no semantic mapping for Codex item.type %r; skipping", item_type)
    return []


def _command_events(item: dict, *, started: bool, completed: bool) -> list[AgentEvent]:
    """Map a ``command_execution`` item to COMMAND (start) / TOOL_RESULT (end)."""
    command = _str(item.get("command"))
    if started:
        return [AgentEvent(kind=EventKind.COMMAND, text=command or "command", tool="shell")]
    if completed:
        output = _truncate(_str(item.get("aggregated_output")))
        exit_code = item.get("exit_code")
        is_error = isinstance(exit_code, int) and exit_code != 0
        return [AgentEvent(kind=EventKind.TOOL_RESULT, text=output, is_error=is_error)]
    return []  # item.updated — no distinct event


def _file_change_events(item: dict) -> list[AgentEvent]:
    """Fan a ``file_change`` item out into one FILE_WRITE per changed path."""
    changes = item.get("changes")
    if not isinstance(changes, list):
        return []
    events: list[AgentEvent] = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        path = _str(change.get("path"))
        if not path:
            continue
        kind = _str(change.get("kind")) or "change"
        events.append(
            AgentEvent(
                kind=EventKind.FILE_WRITE,
                text=f"{kind} {path}",
                path=path,
                data=dict(change),
            )
        )
    return events


def _failure_event(message: str, data: dict) -> AgentEvent:
    """Build a terminal/failed ERROR event with a sensible fallback message."""
    return AgentEvent(
        kind=EventKind.ERROR,
        text=message or "run failed",
        is_error=True,
        data=data if isinstance(data, dict) else {},
    )


def _nested_message(error: object) -> str:
    """Pull ``message`` out of a nested ``{"error": {"message": ...}}`` value."""
    if isinstance(error, dict):
        return _str(error.get("message"))
    return _str(error)


def _str(value: object) -> str:
    """Coerce a value to a string, mapping ``None`` to the empty string."""
    return value if isinstance(value, str) else ("" if value is None else str(value))


def _truncate(text: str) -> str:
    """Trim long command output to a single printable line."""
    text = text.strip()
    return text[:_MAX_RESULT_CHARS] + "…" if len(text) > _MAX_RESULT_CHARS else text
