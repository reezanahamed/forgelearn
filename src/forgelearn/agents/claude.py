"""Adapter that drives the Claude Code CLI (``claude``) in headless mode.

It shells out to ``claude -p <prompt> --output-format stream-json --verbose``
inside a per-session workspace and yields each newline-delimited JSON event as
a :class:`~forgelearn.agents.base.RawEvent`. Flags and behaviour were verified
against Claude Code v2.1.206 and the official headless docs
(https://code.claude.com/docs/en/headless).

The ``stream-json`` protocol emits one JSON object per line:
``system/init`` first, then ``assistant`` messages (whose ``content`` holds
``text`` narration and ``tool_use`` file-writes/commands), ``user`` messages
carrying ``tool_result`` payloads, and a terminal ``result`` event with
``is_error`` and the final text. ``--verbose`` is REQUIRED for ``stream-json``.

The subprocess transport lives in :mod:`forgelearn.agents.process` (shared with
the other providers, Phase 6); this module only builds the command line and
parses Claude's line schema.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

from forgelearn.agents.base import AgentAdapter, RawEvent
from forgelearn.agents.process import stream_cli
from forgelearn.common.logging import get_logger
from forgelearn.config import get_settings

_logger = get_logger("agents.claude")

# --- Fixed Claude Code protocol flags (NOT user settings) ---------------------
# These are hard-wired because the event parser depends on them: change the
# output format and the stream stops being the newline-delimited JSON we parse.
_PRINT_FLAG = "-p"
_OUTPUT_FORMAT_FLAG = "--output-format"
_STREAM_JSON = "stream-json"
_VERBOSE_FLAG = "--verbose"  # required by the CLI whenever --output-format=stream-json
_MODEL_FLAG = "--model"
_ALLOWED_TOOLS_FLAG = "--allowedTools"
_SKIP_PERMISSIONS_FLAG = "--dangerously-skip-permissions"
_BARE_FLAG = "--bare"

# stream-json event vocabulary we key on (see module docstring).
_RESULT_TYPE = "result"
_IS_ERROR_FIELD = "is_error"


class ClaudeAgent(AgentAdapter):
    """Headless driver for the Claude Code CLI."""

    name = "claude"

    def build_argv(self, prompt: str) -> list[str]:
        """Assemble the ``claude`` command line from central config.

        Split out from :meth:`run` so the exact flags can be unit-tested
        without spawning a subprocess.

        Args:
            prompt: The instruction passed to the agent.

        Returns:
            The full argv list, command first.
        """
        settings = get_settings()
        argv: list[str] = [
            settings.agent_cli_command,
            _PRINT_FLAG,
            prompt,
            _OUTPUT_FORMAT_FLAG,
            _STREAM_JSON,
            _VERBOSE_FLAG,
        ]
        if settings.agent_bare:
            argv.append(_BARE_FLAG)
        if settings.agent_skip_permissions:
            argv.append(_SKIP_PERMISSIONS_FLAG)
        if settings.agent_model:
            argv += [_MODEL_FLAG, settings.agent_model]
        allowed = settings.agent_allowed_tools.strip()
        if allowed:
            argv += [_ALLOWED_TOOLS_FLAG, allowed]
        return argv

    def run(self, prompt: str, workspace: Path) -> Iterator[RawEvent]:
        """Run Claude Code on ``prompt`` in ``workspace``; stream its events.

        See :meth:`AgentAdapter.run`. The agent's working directory is
        ``workspace``, so any files it creates appear there. The subprocess
        lifecycle is handled by :func:`forgelearn.agents.process.stream_cli`.

        Args:
            prompt: The natural-language instruction for the agent.
            workspace: An existing directory to run inside.

        Yields:
            One :class:`RawEvent` per JSON line the CLI emits.

        Raises:
            WorkspaceError: If ``workspace`` does not exist or is not a directory.
            AgentError: If the CLI is not installed, the process exits non-zero,
                it times out, or the terminal ``result`` event reports an error.
        """
        argv = self.build_argv(prompt)
        _logger.info("running agent %r in %s", self.name, workspace)
        yield from stream_cli(
            argv,
            Path(workspace),
            get_settings().agent_timeout_seconds,
            self._parse_line,
            is_failure=self._is_error_result,
        )

    @staticmethod
    def _is_error_result(event: RawEvent) -> bool:
        """True for the terminal ``result`` event when it reports an error."""
        return event.type == _RESULT_TYPE and bool(event.data.get(_IS_ERROR_FIELD))

    @staticmethod
    def _parse_line(line: str) -> RawEvent | None:
        """Decode one JSON line into a :class:`RawEvent`, skipping junk.

        A malformed line is logged and dropped rather than aborting the whole
        stream — robustness to partial/streamed output.
        """
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            _logger.warning("skipping non-JSON agent output: %s", line[:200])
            return None
        if not isinstance(data, dict):
            _logger.warning("skipping non-object agent event: %s", line[:200])
            return None
        return RawEvent(
            type=str(data.get("type", "unknown")),
            subtype=data.get("subtype"),
            data=data,
        )
