"""Unit tests for raw→semantic event normalization (Phase 2).

These feed synthetic :class:`RawEvent` objects shaped like the Claude Code
``stream-json`` output through the normalizer, so they run fast and offline. The
real-CLI end-to-end check lives in ``test_agents_integration.py``.
"""

from __future__ import annotations

from forgelearn.agents import stream_events, to_events
from forgelearn.agents.base import RawEvent
from forgelearn.common.types import AgentEvent, EventKind


def _raw(type_: str, **data: object) -> RawEvent:
    """Build a RawEvent whose ``data`` also carries ``type`` (as the CLI emits)."""
    payload = {"type": type_, **data}
    return RawEvent(type=type_, subtype=payload.get("subtype"), data=payload)


def _assistant(*blocks: dict, session_id: str = "sess-1") -> RawEvent:
    return _raw(
        "assistant",
        session_id=session_id,
        message={"role": "assistant", "content": list(blocks)},
    )


def test_init_becomes_a_system_event() -> None:
    """A system/init line maps to one SYSTEM event carrying the model + session."""
    raw = _raw("system", subtype="init", model="claude-opus-4-8", session_id="sess-1")
    events = to_events(raw)
    assert [e.kind for e in events] == [EventKind.SYSTEM]
    assert "claude-opus-4-8" in events[0].text
    assert events[0].session_id == "sess-1"


def test_text_block_becomes_narration() -> None:
    """An assistant text block maps to NARRATION with the stripped text."""
    events = to_events(_assistant({"type": "text", "text": "  Let's build it.  "}))
    assert [e.kind for e in events] == [EventKind.NARRATION]
    assert events[0].text == "Let's build it."


def test_write_tool_use_becomes_file_write_with_path() -> None:
    """A Write tool_use maps to FILE_WRITE and surfaces the file path."""
    events = to_events(
        _assistant(
            {
                "type": "tool_use",
                "name": "Write",
                "input": {"file_path": "hello.py", "content": "print('hi')"},
            }
        )
    )
    assert len(events) == 1
    event = events[0]
    assert event.kind == EventKind.FILE_WRITE
    assert event.tool == "Write"
    assert event.path == "hello.py"
    assert "hello.py" in event.text


def test_bash_tool_use_becomes_command() -> None:
    """A Bash tool_use maps to COMMAND carrying the command string as text."""
    events = to_events(
        _assistant(
            {
                "type": "tool_use",
                "name": "Bash",
                "input": {"command": "python hello.py", "description": "run it"},
            }
        )
    )
    assert events[0].kind == EventKind.COMMAND
    assert events[0].text == "python hello.py"


def test_other_tool_use_becomes_generic_tool() -> None:
    """A non-file, non-shell tool (Read) maps to the generic TOOL kind."""
    events = to_events(
        _assistant(
            {"type": "tool_use", "name": "Read", "input": {"file_path": "hello.py"}}
        )
    )
    assert events[0].kind == EventKind.TOOL
    assert events[0].tool == "Read"
    assert "hello.py" in events[0].text


def test_one_assistant_message_fans_out_to_many_events() -> None:
    """Narration + a tool_use in the same message yield two ordered events."""
    events = to_events(
        _assistant(
            {"type": "text", "text": "Creating the file."},
            {"type": "tool_use", "name": "Write", "input": {"file_path": "a.py"}},
        )
    )
    assert [e.kind for e in events] == [EventKind.NARRATION, EventKind.FILE_WRITE]


def test_tool_result_flattens_block_list_and_flags_errors() -> None:
    """A user tool_result becomes TOOL_RESULT with flattened text + error flag."""
    raw = _raw(
        "user",
        session_id="sess-1",
        message={
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "is_error": True,
                    "content": [{"type": "text", "text": "boom"}],
                }
            ],
        },
    )
    events = to_events(raw)
    assert events[0].kind == EventKind.TOOL_RESULT
    assert events[0].text == "boom"
    assert events[0].is_error is True


def test_result_success_becomes_done() -> None:
    """A successful terminal result maps to DONE with the final text."""
    raw = _raw(
        "result", subtype="success", is_error=False, result="all set", session_id="s"
    )
    events = to_events(raw)
    assert events[0].kind == EventKind.DONE
    assert events[0].is_error is False
    assert events[0].text == "all set"


def test_result_error_becomes_error() -> None:
    """An error terminal result maps to the ERROR kind."""
    events = to_events(_raw("result", subtype="error", is_error=True, result=""))
    assert events[0].kind == EventKind.ERROR
    assert events[0].is_error is True
    assert events[0].text  # falls back to a non-empty message


def test_malformed_and_unknown_events_are_skipped_not_fatal() -> None:
    """Missing content, unknown types, and junk blocks drop out silently."""
    assert to_events(_raw("assistant")) == []  # no message/content
    assert to_events(_assistant({"type": "thinking", "thinking": "…"})) == []
    assert to_events(_raw("mystery_type", foo=1)) == []
    assert to_events(_raw("system", subtype="plugin_install")) == []


def test_stream_events_yields_a_printable_ordered_list() -> None:
    """A full mini-session normalizes to an ordered, labeled, printable stream."""
    raw_stream = [
        _raw("system", subtype="init", model="claude-opus-4-8", session_id="s"),
        _assistant({"type": "text", "text": "Building hello.py."}),
        _assistant(
            {"type": "tool_use", "name": "Write", "input": {"file_path": "hello.py"}}
        ),
        _raw("result", subtype="success", is_error=False, result="done", session_id="s"),
    ]
    events = list(stream_events(raw_stream))
    assert [e.kind for e in events] == [
        EventKind.SYSTEM,
        EventKind.NARRATION,
        EventKind.FILE_WRITE,
        EventKind.DONE,
    ]
    # "print with labels, live, not one blob" — each event renders its own line.
    rendered = [e.pretty() for e in events]
    assert rendered[1] == "[NARRATION] Building hello.py."
    assert rendered[2] == "[FILE_WRITE] Write hello.py"
    assert all(isinstance(e, AgentEvent) for e in events)
