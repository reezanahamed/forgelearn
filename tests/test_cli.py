"""Tests for the ``forgelearn`` command-line entrypoint.

Focus on the startup preflight: ForgeLearn must fail *cleanly* (a friendly message
and a non-zero exit) when its port is already in use, rather than crashing with
uvicorn's terse bind error — and it must never reach the blocking server run in
that case.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest

from forgelearn import cli
from forgelearn.config import get_settings


@pytest.fixture()
def busy_port() -> Iterator[int]:
    """Bind and hold a real port so the preflight sees it as in use."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    try:
        yield sock.getsockname()[1]
    finally:
        sock.close()


def _free_port() -> int:
    """Pick a currently-free port (best effort — closed before returning)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_preflight_flags_a_busy_port(busy_port: int) -> None:
    """A port in use yields an actionable message naming FORGELEARN_PORT."""
    message = cli._preflight_bind("127.0.0.1", busy_port)
    assert message is not None
    assert "already in use" in message
    assert "FORGELEARN_PORT" in message


def test_preflight_passes_for_a_free_port() -> None:
    """A free port preflights clean (no message)."""
    assert cli._preflight_bind("127.0.0.1", _free_port()) is None


def test_main_exits_nonzero_without_starting_when_port_busy(
    busy_port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """main() reports the busy port and never reaches the blocking server run."""
    settings = get_settings()
    monkeypatch.setattr(settings, "host", "127.0.0.1")
    monkeypatch.setattr(settings, "port", busy_port)

    def _must_not_run() -> None:  # pragma: no cover - asserts it isn't called
        raise AssertionError("run_server must not be called when the port is busy")

    monkeypatch.setattr(cli, "run_server", _must_not_run)

    assert cli.main() == cli._EXIT_STARTUP_FAILED
