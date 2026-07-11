"""Smoke + unit tests for the agent-adapter layer (Phase 1).

These never spawn the real CLI, so they run fast and offline. The end-to-end
"agent actually creates a file" check lives in ``test_agents_integration.py``,
which is gated behind an opt-in env var.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forgelearn.agents import (
    ClaudeAgent,
    CodexAgent,
    RawEvent,
    available_agents,
    get_agent,
)
from forgelearn.agents.base import AgentAdapter
from forgelearn.common.errors import AgentError, WorkspaceError
from forgelearn.config import Settings


def test_registry_lists_both_providers() -> None:
    """Both shipped providers are registered and discoverable (Phase 6)."""
    assert "claude" in available_agents()
    assert "codex" in available_agents()


def test_get_agent_returns_claude_adapter() -> None:
    """The factory yields a concrete AgentAdapter for a known name."""
    agent = get_agent("claude")
    assert isinstance(agent, ClaudeAgent)
    assert isinstance(agent, AgentAdapter)
    assert agent.name == "claude"


def test_get_agent_rejects_unknown_provider() -> None:
    """An unknown provider name raises AgentError, not KeyError."""
    with pytest.raises(AgentError):
        get_agent("nope")


def test_build_argv_uses_stream_json_and_verbose() -> None:
    """The command line always requests stream-json with --verbose."""
    argv = ClaudeAgent().build_argv("make hello.py")
    assert argv[0] == "claude"
    assert "-p" in argv
    assert "make hello.py" in argv
    # --verbose is mandatory whenever --output-format=stream-json is used.
    i = argv.index("--output-format")
    assert argv[i + 1] == "stream-json"
    assert "--verbose" in argv


def test_build_argv_honors_config_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model / allowed-tools / bare flags are driven by central config."""
    tuned = Settings(
        agent_model="sonnet",
        agent_allowed_tools="Write,Edit",
        agent_bare=True,
        agent_skip_permissions=False,
    )
    monkeypatch.setattr("forgelearn.agents.claude.get_settings", lambda: tuned)
    argv = ClaudeAgent().build_argv("do it")
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert argv[argv.index("--allowedTools") + 1] == "Write,Edit"
    assert "--bare" in argv
    assert "--dangerously-skip-permissions" not in argv


def test_run_rejects_missing_workspace(tmp_path: Path) -> None:
    """A non-existent workspace fails fast with WorkspaceError."""
    missing = tmp_path / "does-not-exist"
    with pytest.raises(WorkspaceError):
        # Generators are lazy — consume it to trigger the check.
        list(ClaudeAgent().run("hi", missing))


def test_parse_line_wraps_json_event() -> None:
    """A well-formed JSON line becomes a typed RawEvent."""
    event = ClaudeAgent._parse_line('{"type":"assistant","subtype":null,"x":1}')
    assert isinstance(event, RawEvent)
    assert event.type == "assistant"
    assert event.data["x"] == 1


def test_parse_line_skips_malformed_output() -> None:
    """Non-JSON / non-object lines are dropped, not fatal."""
    assert ClaudeAgent._parse_line("not json at all") is None
    assert ClaudeAgent._parse_line("[1, 2, 3]") is None


# --- Codex provider (Phase 6) ------------------------------------------------


def test_get_agent_returns_codex_adapter() -> None:
    """The factory yields a distinct CodexAgent for the codex name."""
    agent = get_agent("codex")
    assert isinstance(agent, CodexAgent)
    assert isinstance(agent, AgentAdapter)
    assert agent.name == "codex"


def test_codex_build_argv_uses_exec_json_headless_flags() -> None:
    """Codex runs `exec --json` headless, skips the git check, prompt last."""
    argv = CodexAgent().build_argv("make hello.py")
    assert argv[0] == "codex"
    assert argv[1] == "exec"
    assert "--json" in argv
    assert "--skip-git-repo-check" in argv
    # skip-permissions is on by default → the full bypass flag, no sandbox pair.
    assert "--dangerously-bypass-approvals-and-sandbox" in argv
    assert "--sandbox" not in argv
    # The prompt is positional and must be last so it can't be read as a flag value.
    assert argv[-1] == "make hello.py"


def test_codex_build_argv_sandboxes_when_permissions_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without skip-permissions, Codex gets a writable sandbox + no approvals."""
    tuned = Settings(agent_skip_permissions=False, codex_model="gpt-5-codex")
    monkeypatch.setattr("forgelearn.agents.codex.get_settings", lambda: tuned)
    argv = CodexAgent().build_argv("do it")
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert argv[argv.index("--ask-for-approval") + 1] == "never"
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv
    assert argv[argv.index("--model") + 1] == "gpt-5-codex"


def test_codex_parse_line_wraps_and_skips() -> None:
    """A Codex JSONL line becomes a RawEvent; junk is dropped."""
    event = CodexAgent._parse_line('{"type":"turn.completed","usage":{}}')
    assert isinstance(event, RawEvent)
    assert event.type == "turn.completed"
    assert CodexAgent._parse_line("not json") is None
    assert CodexAgent._parse_line('"a bare string"') is None
