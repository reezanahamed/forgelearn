"""Run a workspace's project as a subprocess and stream its output (Phase 5).

The Run action executes the resolved entrypoint (see
:func:`forgelearn.workspace.manager.find_entrypoint`) and turns its live output
into the SAME normalized :class:`~forgelearn.common.types.AgentEvent` vocabulary
the agent stream already speaks, so the browser renders a Run with no new client
code:

* one ``COMMAND`` event announcing the command line,
* a ``TOOL_RESULT`` event per output line (stdout and stderr merged, in order),
* a terminal ``DONE`` (exit 0) or ``ERROR`` (non-zero, timeout, or launch fail).

The subprocess is started in its own session/process-group and killed as a group
on timeout so a hung child (or its children) can't outlive the Run. Output is
read line-by-line from a merged pipe, so the browser sees it as it is produced
rather than in one blob at the end.

⚠️ SANDBOXING TODO (PLAN §10): this runs arbitrary user code with the server's
own permissions. Acceptable for the local single-user MVP only; multi-user
serving must isolate each Run (container/jail) before exposure.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import suppress
from pathlib import Path

from forgelearn.common.errors import WorkspaceError
from forgelearn.common.logging import get_logger
from forgelearn.common.types import AgentEvent, EventKind
from forgelearn.config import get_settings
from forgelearn.workspace.manager import find_entrypoint, workspace_path

_logger = get_logger("workspace.runner")

# The interpreter used to run a project's Python entrypoint. Using the current
# interpreter (not a bare "python" string) guarantees the Run uses the same
# environment ForgeLearn is installed in, and avoids a python/python3 mismatch.
_PYTHON = sys.executable

# Force the child's stdout/stderr to be unbuffered so its prints stream to the
# browser immediately instead of sitting in a block buffer until it exits.
# ``bufsize=1`` only line-buffers OUR end of the pipe; a Python child whose stdout
# is a pipe (not a TTY) block-buffers unless told otherwise, so we set this.
_UNBUFFERED_ENV = "PYTHONUNBUFFERED"

# Grace period (seconds) between SIGTERM and the SIGKILL fallback when killing a
# timed-out process group, so a well-behaved child can flush and exit cleanly.
_KILL_GRACE_SECONDS = 3


def run_workspace(session_id: str) -> Iterator[AgentEvent]:
    """Execute the session's project and yield its activity as events.

    Args:
        session_id: The session whose workspace to run.

    Yields:
        A ``COMMAND`` event, then one ``TOOL_RESULT`` per output line, then a
        terminal ``DONE`` or ``ERROR`` event.

    Raises:
        WorkspaceError: If the session is invalid or has nothing runnable. (The
            server bridge converts this into a terminal error frame, so it is
            surfaced before any event is yielded.)
    """
    entrypoint = find_entrypoint(session_id)
    root = workspace_path(session_id)
    # Run by workspace-relative path (cwd is the workspace), so a nested
    # entrypoint like ``pkg/main.py`` runs correctly too.
    rel = entrypoint.relative_to(root)
    argv = [_PYTHON, rel.as_posix()]

    display = f"python {rel.as_posix()}"
    _logger.info("running project for session %r: %s", session_id, display)
    yield AgentEvent(kind=EventKind.COMMAND, text=display, tool="run", path=rel.as_posix())

    yield from _stream_process(argv, root, get_settings().run_timeout_seconds)


def _stream_process(
    argv: list[str], cwd: Path, timeout_seconds: int
) -> Iterator[AgentEvent]:
    """Spawn ``argv`` in ``cwd``, stream merged output, enforce a timeout.

    stdout and stderr are merged into one pipe so the learner sees output and
    errors interleaved in the order they happened. A watchdog kills the whole
    process group on timeout. The final event reflects how the process ended.
    """
    env = {**os.environ, _UNBUFFERED_ENV: "1"}
    timed_out = threading.Event()

    try:
        proc = subprocess.Popen(  # noqa: S603 - argv is [interpreter, file], no shell
            argv,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # merge stderr into stdout, preserving order
            text=True,
            bufsize=1,  # line-buffered
            env=env,
            start_new_session=True,  # own process group, so we can kill children too
        )
    except OSError as exc:
        yield AgentEvent(
            kind=EventKind.ERROR, text=f"failed to start run: {exc}", is_error=True
        )
        return

    def _terminate(mark_timeout: bool) -> None:
        """Stop the whole process group: SIGTERM, brief grace, then SIGKILL.

        ``start_new_session=True`` made the child a group leader (pgid == pid), so
        signalling the group reaps any grandchildren the user's script spawned.
        """
        if mark_timeout:
            timed_out.set()
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(_KILL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)

    watchdog = threading.Timer(timeout_seconds, _terminate, args=(True,))
    watchdog.start()
    try:
        assert proc.stdout is not None  # PIPE guarantees this
        for line in proc.stdout:
            text = line.rstrip("\n")
            yield AgentEvent(kind=EventKind.TOOL_RESULT, text=text, tool="run")
        returncode = proc.wait()
    finally:
        watchdog.cancel()
        if proc.poll() is None:  # consumer stopped early — don't orphan the child
            _terminate(mark_timeout=False)
        with suppress(OSError):
            proc.stdout.close()  # release the pipe fd
        proc.wait()  # reap so returncode is set and no zombie lingers

    if timed_out.is_set():
        yield AgentEvent(
            kind=EventKind.ERROR,
            text=f"run timed out after {timeout_seconds}s and was stopped",
            is_error=True,
        )
    elif returncode == 0:
        yield AgentEvent(kind=EventKind.DONE, text="run finished (exit 0)")
    else:
        yield AgentEvent(
            kind=EventKind.ERROR,
            text=f"run exited with code {returncode}",
            is_error=True,
        )
