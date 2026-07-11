"""End-to-end check: the Claude adapter really builds a file (Phase 1 Done-when).

This spawns the actual ``claude`` CLI, so it costs tokens and needs the binary
installed and authenticated. It is skipped by default and only runs when
``FORGELEARN_RUN_AGENT_TESTS=1`` is set AND the CLI is on PATH — keeping the
normal ``pytest`` run fast, free, and offline.

    FORGELEARN_RUN_AGENT_TESTS=1 pytest tests/test_agents_integration.py -s
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from forgelearn.agents import ClaudeAgent, get_agent, stream_events
from forgelearn.agents.base import RawEvent
from forgelearn.common.types import EventKind

_ENABLED = os.getenv("FORGELEARN_RUN_AGENT_TESTS") == "1"
_HAVE_CLI = shutil.which(ClaudeAgent().build_argv("x")[0]) is not None

pytestmark = pytest.mark.skipif(
    not (_ENABLED and _HAVE_CLI),
    reason="set FORGELEARN_RUN_AGENT_TESTS=1 and install the agent CLI to run",
)


def test_agent_builds_a_real_file(tmp_path: Path) -> None:
    """Ask the agent to create hello.py; assert BOTH steps stream AND the file lands."""
    workspace = tmp_path / "session"
    workspace.mkdir()

    events: list[RawEvent] = list(
        get_agent("claude").run(
            "Create a file named hello.py that prints hello. Do nothing else.",
            workspace,
        )
    )

    # The adapter returned the agent's steps...
    assert events, "expected a stream of agent events"
    types = {e.type for e in events}
    assert "assistant" in types, "expected assistant narration/tool events"
    assert any(e.type == "result" for e in events), "expected a terminal result event"
    assert not events[-1].data.get("is_error", False), "run reported an error result"

    # ...and the real file appeared in the workspace.
    hello = workspace / "hello.py"
    assert hello.is_file(), f"agent did not create {hello}"
    assert "hello" in hello.read_text().lower()

    # Phase 2: the same raw stream normalizes to clean, typed, labeled events —
    # narration + a file write + a terminal DONE, not one opaque blob.
    semantic = list(stream_events(events))
    kinds = {e.kind for e in semantic}
    assert EventKind.NARRATION in kinds, "expected narration events"
    assert EventKind.FILE_WRITE in kinds, "expected a file-write event"
    assert semantic[-1].kind == EventKind.DONE, "expected a terminal DONE event"
    assert any(e.path and e.path.endswith("hello.py") for e in semantic), (
        "file-write event should surface the hello.py path"
    )
    for event in semantic:  # every event is printable with a label
        assert event.pretty().startswith(f"[{event.label}]")
