"""Unit tests for the Codex → AgentEvent normalizer (Phase 6).

These are fast and offline: they feed hand-built :class:`RawEvent` lines shaped
like real ``codex exec --json`` output and assert they land on the same semantic
:class:`AgentEvent` vocabulary the Claude normalizer produces. No subprocess.
"""

from __future__ import annotations

from forgelearn.agents.base import RawEvent
from forgelearn.agents.codex_events import stream_events, to_events
from forgelearn.common.types import EventKind


def _raw(type_: str, **data: object) -> RawEvent:
    """Build a Codex RawEvent whose payload includes its own ``type`` field."""
    return RawEvent(type=type_, data={"type": type_, **data})


def _item(phase: str, item: dict) -> RawEvent:
    """Build an ``item.<phase>`` RawEvent wrapping ``item``."""
    return _raw(f"item.{phase}", item=item)


def test_thread_started_maps_to_system_with_thread_id() -> None:
    """thread.started becomes a SYSTEM event carrying the thread id as session."""
    (event,) = to_events(_raw("thread.started", thread_id="abc"))
    assert event.kind == EventKind.SYSTEM
    assert event.session_id == "abc"


def test_agent_message_completed_is_narration() -> None:
    """A completed agent_message item is the teaching NARRATION voice."""
    events = to_events(_item("completed", {"type": "agent_message", "text": "Building it"}))
    assert [e.kind for e in events] == [EventKind.NARRATION]
    assert events[0].text == "Building it"


def test_agent_message_started_yields_nothing() -> None:
    """Only the completed agent_message emits; earlier phases are ignored."""
    assert to_events(_item("started", {"type": "agent_message", "text": "x"})) == []


def test_command_execution_start_and_complete() -> None:
    """command_execution → COMMAND on start, TOOL_RESULT (with output) on completion."""
    started = to_events(
        _item("started", {"type": "command_execution", "command": "ls", "status": "in_progress"})
    )
    assert started[0].kind == EventKind.COMMAND
    assert started[0].text == "ls"

    done = to_events(
        _item(
            "completed",
            {
                "type": "command_execution",
                "command": "ls",
                "aggregated_output": "hello.py\n",
                "exit_code": 0,
                "status": "completed",
            },
        )
    )
    assert done[0].kind == EventKind.TOOL_RESULT
    assert done[0].text == "hello.py"
    assert done[0].is_error is False


def test_command_nonzero_exit_flags_error() -> None:
    """A non-zero exit code marks the TOOL_RESULT as an error."""
    (event,) = to_events(
        _item(
            "completed",
            {"type": "command_execution", "command": "false", "aggregated_output": "boom", "exit_code": 1},
        )
    )
    assert event.kind == EventKind.TOOL_RESULT
    assert event.is_error is True


def test_file_change_fans_out_to_file_writes() -> None:
    """A file_change item yields one FILE_WRITE per changed path."""
    events = to_events(
        _item(
            "completed",
            {
                "type": "file_change",
                "changes": [
                    {"path": "a.py", "kind": "add"},
                    {"path": "b.py", "kind": "update"},
                ],
                "status": "completed",
            },
        )
    )
    assert [e.kind for e in events] == [EventKind.FILE_WRITE, EventKind.FILE_WRITE]
    assert [e.path for e in events] == ["a.py", "b.py"]


def test_turn_completed_is_done() -> None:
    """turn.completed is the terminal success event."""
    (event,) = to_events(_raw("turn.completed", usage={"output_tokens": 3}))
    assert event.kind == EventKind.DONE


def test_turn_failed_and_error_map_to_error() -> None:
    """turn.failed and top-level error both become terminal ERROR events."""
    (failed,) = to_events(_raw("turn.failed", error={"message": "model died"}))
    assert failed.kind == EventKind.ERROR
    assert failed.is_error is True
    assert "model died" in failed.text

    (err,) = to_events(_raw("error", message="broken pipe"))
    assert err.kind == EventKind.ERROR
    assert "broken pipe" in err.text


def test_reasoning_and_todo_are_hidden_system_events() -> None:
    """Internal reasoning / todo items are SYSTEM (the UI hides them)."""
    (reasoning,) = to_events(_item("completed", {"type": "reasoning", "text": "hmm"}))
    assert reasoning.kind == EventKind.SYSTEM
    (todo,) = to_events(_item("completed", {"type": "todo_list", "items": []}))
    assert todo.kind == EventKind.SYSTEM


def test_unknown_and_malformed_items_are_skipped() -> None:
    """Unknown item types, non-dict items, and unknown top types are dropped."""
    assert to_events(_item("completed", {"type": "future_thing"})) == []
    assert to_events(RawEvent(type="item.completed", data={"item": "nope"})) == []
    assert to_events(_raw("turn.started")) == []
    assert to_events(_raw("totally.unknown")) == []


def test_stream_orders_events_and_ends_on_done() -> None:
    """A full session normalizes to ordered events terminating in DONE."""
    raws = [
        _raw("thread.started", thread_id="t"),
        _item("completed", {"type": "agent_message", "text": "make hello.py"}),
        _item("completed", {"type": "file_change", "changes": [{"path": "hello.py", "kind": "add"}]}),
        _raw("turn.completed", usage={}),
    ]
    events = list(stream_events(raws))
    kinds = [e.kind for e in events]
    assert EventKind.NARRATION in kinds
    assert EventKind.FILE_WRITE in kinds
    assert events[-1].kind == EventKind.DONE


def test_stream_appends_synthetic_terminal_when_missing() -> None:
    """If the stream ends without a terminal event, a DONE is synthesized.

    The browser closes its SSE connection on the terminal event, so the
    normalizer must always end on one even if Codex's schema drifts.
    """
    events = list(stream_events([_item("completed", {"type": "agent_message", "text": "hi"})]))
    assert events[-1].kind == EventKind.DONE
