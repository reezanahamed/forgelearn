"""Shared subprocess driver for headless CLI coding agents (Phase 6).

Every provider adapter (Claude Code, Codex, …) runs *some* CLI as a subprocess
and reads its newline-delimited output line by line. That transport — spawn,
stream stdout, enforce a wall-clock timeout, capture stderr, reap the child, and
translate a failure into an :class:`~forgelearn.common.errors.AgentError` — is
identical across providers. It lived inline in ``claude.py`` through Phase 5;
Phase 6 extracts it here so a second adapter reuses it instead of copy-pasting
the (fiddly, easy-to-get-wrong) process hygiene (DRY, per CLAUDE.md standards).

What stays provider-specific and is injected as callbacks:

* ``parse_line`` — decode one output line into a :class:`RawEvent` (each CLI has
  its own JSON schema), or ``None`` to skip junk.
* ``is_failure`` — decide whether a parsed event signals a failed run, so the
  driver can raise after the stream drains (e.g. Claude's ``result.is_error``,
  Codex's ``turn.failed``). Optional; process exit code is always honored.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterator
from pathlib import Path

from forgelearn.agents.base import RawEvent
from forgelearn.common.errors import AgentError, WorkspaceError
from forgelearn.common.logging import get_logger

_logger = get_logger("agents.process")

# Type of the per-provider callbacks injected into the generic driver.
ParseLine = Callable[[str], RawEvent | None]
IsFailure = Callable[[RawEvent], bool]

_STDERR_TAIL_CHARS = 1000  # how much captured stderr to surface in an error


def stream_cli(
    argv: list[str],
    workspace: Path,
    timeout_seconds: int,
    parse_line: ParseLine,
    *,
    is_failure: IsFailure | None = None,
) -> Iterator[RawEvent]:
    """Run ``argv`` in ``workspace`` and stream its output as :class:`RawEvent`.

    Provider-agnostic transport: it validates the workspace and that the binary
    is on ``PATH``, spawns the process with stdout piped and stderr captured to a
    temp file (so a full stderr pipe can't deadlock the stdout reader), parses
    each stdout line with ``parse_line``, and yields the events live. A watchdog
    timer kills a hung process; after the stream drains it raises on timeout,
    non-zero exit, or an ``is_failure`` event.

    Args:
        argv: The command line, binary first (built by the adapter from config).
        workspace: An existing directory to run inside; the agent's files land here.
        timeout_seconds: Hard wall-clock limit before the process is killed.
        parse_line: Decodes one output line into a :class:`RawEvent`, or ``None``
            to drop a blank/malformed line without aborting the stream.
        is_failure: Optional predicate marking a parsed event as a failed-run
            signal; when it returns True the driver raises after the stream ends.

    Yields:
        One :class:`RawEvent` per meaningful output line, in order.

    Raises:
        WorkspaceError: If ``workspace`` does not exist or is not a directory.
        AgentError: If the CLI is not installed, the process exits non-zero,
            it times out, or an ``is_failure`` event was seen.
    """
    workspace = Path(workspace)
    if not workspace.is_dir():
        raise WorkspaceError(f"workspace is not an existing directory: {workspace}")
    if shutil.which(argv[0]) is None:
        raise AgentError(
            f"agent CLI {argv[0]!r} not found on PATH; install it or set the "
            "provider's CLI command in config"
        )

    _logger.debug("spawning agent process in %s: %s", workspace, argv)
    yield from _stream(argv, workspace, timeout_seconds, parse_line, is_failure)


def _stream(
    argv: list[str],
    workspace: Path,
    timeout_seconds: int,
    parse_line: ParseLine,
    is_failure: IsFailure | None,
) -> Iterator[RawEvent]:
    """Spawn the process, parse stdout line-by-line, and enforce the timeout.

    stderr is captured to a temp file (avoids a pipe-buffer deadlock while we
    read stdout) and surfaced only if the run fails. A watchdog timer kills a
    hung process so a stalled agent can't block forever.
    """
    timed_out = threading.Event()
    failed = False

    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
        try:
            proc = subprocess.Popen(  # noqa: S603 - argv is built from config, not shell
                argv,
                cwd=str(workspace),
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                text=True,
                bufsize=1,  # line-buffered
            )
        except OSError as exc:  # e.g. binary vanished between which() and here
            raise AgentError(f"failed to start agent {argv[0]!r}: {exc}") from exc

        def _kill() -> None:
            timed_out.set()
            proc.kill()

        watchdog = threading.Timer(timeout_seconds, _kill)
        watchdog.start()
        try:
            assert proc.stdout is not None  # PIPE guarantees this
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                event = parse_line(line)
                if event is None:
                    continue
                if is_failure is not None and is_failure(event):
                    failed = True
                yield event
            returncode = proc.wait()
        finally:
            watchdog.cancel()
            if proc.poll() is None:  # consumer stopped early — don't orphan it
                proc.kill()
                proc.wait()

        if timed_out.is_set():
            raise AgentError(f"agent {argv[0]!r} timed out after {timeout_seconds}s")
        if returncode != 0:
            raise AgentError(
                f"agent {argv[0]!r} exited with code {returncode}: "
                f"{_read_stderr(stderr_file)}"
            )
        if failed:
            raise AgentError(
                f"agent {argv[0]!r} reported an error result: "
                f"{_read_stderr(stderr_file)}"
            )


def _read_stderr(stderr_file: "tempfile._TemporaryFileWrapper") -> str:
    """Read captured stderr for an error message (best-effort, truncated)."""
    try:
        stderr_file.seek(0)
        text = stderr_file.read().strip()
    except OSError:
        return "<stderr unavailable>"
    return text[-_STDERR_TAIL_CHARS:] if text else "<no stderr>"
