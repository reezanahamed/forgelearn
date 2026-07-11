"""Adapter that drives the OpenAI Codex CLI (``codex``) in headless mode.

Codex is ForgeLearn's second engine (Phase 6), proving the adapter layer swaps
CLIs without any caller change. It shells out to
``codex exec <prompt> --json --skip-git-repo-check`` inside a per-session
workspace and yields each newline-delimited JSON event as a
:class:`~forgelearn.agents.base.RawEvent`; :mod:`forgelearn.agents.codex_events`
then normalizes Codex's schema onto the same :class:`AgentEvent` vocabulary the
Claude adapter produces.

Flags verified against the official non-interactive docs (July 2026,
https://developers.openai.com/codex/noninteractive and the CLI reference):

* ``codex exec <prompt>`` — run a single task non-interactively, prompt positional.
* ``--json`` — emit newline-delimited JSON events on stdout.
* ``--skip-git-repo-check`` — allow running outside a git repo (our workspaces
  are plain folders, not repos), else Codex refuses to start.
* ``--dangerously-bypass-approvals-and-sandbox`` — run every command without
  approvals or sandboxing (the local single-user MVP posture, mirroring the
  Claude adapter's ``--dangerously-skip-permissions``; PLAN §10). When
  permissions are NOT skipped we instead pass ``--sandbox workspace-write
  --ask-for-approval never`` so it can still write files without pausing for a
  human it cannot reach over a headless pipe.
* ``--model <name>`` — override the model when configured.

Auth is the CLI's own concern: Codex reads ``CODEX_API_KEY`` from the
environment (or a prior ``codex login``), which the subprocess inherits. As with
any provider, the ``codex`` binary must be installed and authenticated on the
host (PLAN §8a deploy caveat).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from forgelearn.agents.base import AgentAdapter, RawEvent
from forgelearn.agents.process import stream_cli
from forgelearn.common.logging import get_logger
from forgelearn.common.types import AgentEvent
from forgelearn.config import get_settings

_logger = get_logger("agents.codex")

# --- Fixed Codex protocol flags (NOT user settings) --------------------------
# Hard-wired because the normalizer depends on them: change --json and the
# stream stops being the newline-delimited JSON codex_events.py parses.
_EXEC_SUBCOMMAND = "exec"
_JSON_FLAG = "--json"
_SKIP_GIT_CHECK_FLAG = "--skip-git-repo-check"
_BYPASS_FLAG = "--dangerously-bypass-approvals-and-sandbox"
_SANDBOX_FLAG = "--sandbox"
_SANDBOX_WORKSPACE_WRITE = "workspace-write"
_APPROVAL_FLAG = "--ask-for-approval"
_APPROVAL_NEVER = "never"
_MODEL_FLAG = "--model"

# Codex event types that signal the whole run failed (see codex_events.py).
_FAILURE_TYPES = frozenset({"turn.failed", "error"})


class CodexAgent(AgentAdapter):
    """Headless driver for the OpenAI Codex CLI."""

    name = "codex"

    def build_argv(self, prompt: str) -> list[str]:
        """Assemble the ``codex exec`` command line from central config.

        Split out from :meth:`run` so the exact flags can be unit-tested without
        spawning a subprocess.

        Args:
            prompt: The instruction passed to the agent.

        Returns:
            The full argv list, command first, prompt last.
        """
        settings = get_settings()
        argv: list[str] = [
            settings.codex_cli_command,
            _EXEC_SUBCOMMAND,
            _JSON_FLAG,
            _SKIP_GIT_CHECK_FLAG,
        ]
        if settings.agent_skip_permissions:
            argv.append(_BYPASS_FLAG)
        else:
            # Let it write files but never pause for approval on a headless pipe.
            argv += [
                _SANDBOX_FLAG,
                _SANDBOX_WORKSPACE_WRITE,
                _APPROVAL_FLAG,
                _APPROVAL_NEVER,
            ]
        if settings.codex_model:
            argv += [_MODEL_FLAG, settings.codex_model]
        # Prompt is positional; keep it last so it can never be read as a flag value.
        argv.append(prompt)
        return argv

    def run(self, prompt: str, workspace: Path) -> Iterator[RawEvent]:
        """Run Codex on ``prompt`` in ``workspace``; stream its raw events.

        See :meth:`AgentAdapter.run`. The subprocess lifecycle is handled by
        :func:`forgelearn.agents.process.stream_cli`.

        Args:
            prompt: The natural-language instruction for the agent.
            workspace: An existing directory to run inside.

        Yields:
            One :class:`RawEvent` per JSON line the CLI emits.

        Raises:
            WorkspaceError: If ``workspace`` does not exist or is not a directory.
            AgentError: If the CLI is not installed, the process exits non-zero,
                it times out, or a ``turn.failed``/``error`` event is emitted.
        """
        argv = self.build_argv(prompt)
        _logger.info("running agent %r in %s", self.name, workspace)
        yield from stream_cli(
            argv,
            Path(workspace),
            get_settings().agent_timeout_seconds,
            self._parse_line,
            is_failure=self._is_failure,
        )

    def run_events(self, prompt: str, workspace: Path) -> Iterator[AgentEvent]:
        """Run Codex and stream normalized :class:`AgentEvent` objects.

        Overrides the base (Anthropic-shaped) normalization with Codex's own,
        so both providers hand the rest of the platform one event vocabulary.

        Args:
            prompt: The natural-language instruction for the agent.
            workspace: An existing directory to run inside.

        Yields:
            :class:`AgentEvent` objects in order, ending on a terminal event.
        """
        from forgelearn.agents.codex_events import stream_events

        yield from stream_events(self.run(prompt, workspace))

    @staticmethod
    def _is_failure(event: RawEvent) -> bool:
        """True for a top-level event that means the whole run failed."""
        return event.type in _FAILURE_TYPES

    @staticmethod
    def _parse_line(line: str) -> RawEvent | None:
        """Decode one JSON line into a :class:`RawEvent`, skipping junk.

        A malformed line is logged and dropped rather than aborting the stream —
        robustness to partial/streamed output.
        """
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            _logger.warning("skipping non-JSON Codex output: %s", line[:200])
            return None
        if not isinstance(data, dict):
            _logger.warning("skipping non-object Codex event: %s", line[:200])
            return None
        return RawEvent(type=str(data.get("type", "unknown")), data=data)
