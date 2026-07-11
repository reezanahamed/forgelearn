"""Command-line entrypoint for ForgeLearn (the ``forgelearn`` command).

Running ``forgelearn`` logs the resolved configuration via the shared logger and
then starts the single FastAPI server (UI + API on one port), staying up until
interrupted (Ctrl+C). The config dump doubles as a startup smoke check that the
plumbing loaded correctly.
"""

from __future__ import annotations

import errno
import socket

from forgelearn import __version__
from forgelearn.common.logging import get_logger
from forgelearn.config import Settings, get_settings
from forgelearn.server import run as run_server

_logger = get_logger("cli")

# Process exit codes.
_EXIT_OK = 0
_EXIT_STARTUP_FAILED = 1


def _preflight_bind(host: str, port: int) -> str | None:
    """Check the server's port is bindable, returning a friendly error if not.

    uvicorn would otherwise fail with a terse ``[Errno 98]`` line and a non-zero
    exit; catching the common cases here lets us tell the user exactly what to do
    (change ``FORGELEARN_PORT``) before the server ever starts.

    Args:
        host: The interface the server will bind to.
        port: The TCP port the server will listen on.

    Returns:
        ``None`` if the port is free to bind, otherwise a human-readable message
        explaining the problem and how to fix it.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Match uvicorn's default so a TIME_WAIT socket isn't a false positive.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            return (
                f"Port {port} is already in use, so ForgeLearn can't start on "
                f"http://{host}:{port}.\n"
                f"  • Free that port (find it with `lsof -i :{port}`), or\n"
                f"  • Start on another port, e.g. `FORGELEARN_PORT=9000 forgelearn`."
            )
        if exc.errno in (errno.EACCES, errno.EPERM):
            return (
                f"No permission to bind port {port} (ports below 1024 usually need "
                f"privileges). Pick a higher port, e.g. `FORGELEARN_PORT=9000 forgelearn`."
            )
        return f"Could not bind {host}:{port}: {exc}"
    finally:
        sock.close()
    return None


def _log_settings(settings: Settings) -> None:
    """Emit each resolved setting through the shared logger."""
    _logger.info("ForgeLearn v%s, loaded configuration:", __version__)
    _logger.info("  host           = %s", settings.host)
    _logger.info("  port           = %s", settings.port)
    _logger.info("  default_agent  = %s", settings.default_agent)
    _logger.info("  workspace_dir  = %s", settings.workspace_dir)
    _logger.info("  sessions_dir   = %s", settings.sessions_dir)
    _logger.info("  log_level      = %s", settings.log_level)


def main() -> int:
    """Load configuration, log it, and start the server (blocking).

    The server runs until interrupted; a clean Ctrl+C is treated as a normal
    shutdown rather than an error.

    Returns:
        Process exit code (0 on success/clean shutdown, non-zero if the server
        could not start — e.g. the port was already in use).
    """
    settings = get_settings()
    _log_settings(settings)

    problem = _preflight_bind(settings.host, settings.port)
    if problem is not None:
        _logger.error("%s", problem)
        return _EXIT_STARTUP_FAILED

    try:
        run_server()
    except KeyboardInterrupt:
        _logger.info("shutting down (interrupted)")
    except SystemExit as exc:
        # uvicorn exits this way if it still fails to start (e.g. the port was
        # taken in the moment after our preflight check). Report it cleanly.
        if exc.code:
            _logger.error(
                "server failed to start (exit %s); if the port is busy, set "
                "FORGELEARN_PORT to a free port.",
                exc.code,
            )
            return _EXIT_STARTUP_FAILED
    return _EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
