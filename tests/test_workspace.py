"""Tests for per-session workspaces: locate/list/read, entrypoint, and Run.

These run fully offline. The Run tests spawn the *current* Python interpreter on
tiny scripts written into a temp workspace — fast, deterministic, no network and
no coding-agent CLI. The workspace root is redirected to a ``tmp_path`` so no
real ``workspaces/`` folder is touched.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from forgelearn.common.errors import WorkspaceError
from forgelearn.common.types import EventKind
from forgelearn.config import get_settings
from forgelearn.workspace import (
    find_entrypoint,
    get_or_create,
    list_files,
    read_file,
    run_workspace,
    workspace_path,
)


@pytest.fixture()
def workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the workspace directory at an isolated temp dir for a test."""
    monkeypatch.setattr(get_settings(), "workspace_dir", tmp_path)
    return tmp_path


def _write(session: str, rel: str, body: str) -> Path:
    """Create a file at ``rel`` inside ``session``'s workspace and return it."""
    root = get_or_create(session)
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(textwrap.dedent(body), encoding="utf-8")
    return target


# --- Locating & creating -----------------------------------------------------


def test_get_or_create_is_stable(workspace_root: Path) -> None:
    """The same session id always maps to the same created directory."""
    first = get_or_create("abc123")
    second = get_or_create("abc123")
    assert first == second == workspace_path("abc123")
    assert first.is_dir()


@pytest.mark.parametrize("bad", ["", "../evil", "a/b", "with.dot", "space id", "x" * 65])
def test_invalid_session_id_rejected(workspace_root: Path, bad: str) -> None:
    """A malformed/traversal-prone session id is refused, not joined onto a path."""
    with pytest.raises(WorkspaceError):
        workspace_path(bad)


# --- Listing -----------------------------------------------------------------


def test_list_files_sorted_and_excludes_cruft(workspace_root: Path) -> None:
    """Listing returns sorted relative posix paths and hides build artifacts."""
    _write("s1", "main.py", "print('hi')")
    _write("s1", "pkg/util.py", "x = 1")
    _write("s1", "__pycache__/main.cpython-311.pyc", "junk")
    _write("s1", "notes.txt", "hello")

    paths = [e.path for e in list_files("s1")]
    assert paths == ["main.py", "notes.txt", "pkg/util.py"]


def test_list_files_missing_workspace_is_empty(workspace_root: Path) -> None:
    """Listing a session that never wrote anything yields an empty list."""
    assert list_files("neverused") == []


# --- Reading -----------------------------------------------------------------


def test_read_file_returns_contents(workspace_root: Path) -> None:
    """A workspace file's text is returned verbatim."""
    _write("s2", "hello.py", "print('hello')\n")
    assert read_file("s2", "hello.py") == "print('hello')\n"


def test_read_file_rejects_traversal(workspace_root: Path) -> None:
    """A path escaping the workspace is refused."""
    _write("s3", "ok.py", "x = 1")
    with pytest.raises(WorkspaceError):
        read_file("s3", "../../../etc/passwd")


def test_read_file_truncates_large_file(
    workspace_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file past the view cap is truncated with a marker."""
    monkeypatch.setattr(get_settings(), "workspace_max_view_bytes", 10)
    _write("s4", "big.txt", "0123456789ABCDEF")
    text = read_file("s4", "big.txt")
    assert text.startswith("0123456789")
    assert "truncated" in text


# --- Entrypoint resolution ---------------------------------------------------


def test_find_entrypoint_prefers_priority_name(workspace_root: Path) -> None:
    """A configured priority name (main.py) wins over other .py files."""
    _write("e1", "helper.py", "x = 1")
    _write("e1", "main.py", "print('go')")
    assert find_entrypoint("e1").name == "main.py"


def test_find_entrypoint_single_file(workspace_root: Path) -> None:
    """With one .py file and no priority match, that file is chosen."""
    _write("e2", "solo.py", "print('solo')")
    assert find_entrypoint("e2").name == "solo.py"


def test_find_entrypoint_none_raises(workspace_root: Path) -> None:
    """A workspace with no .py file has nothing to run."""
    _write("e3", "readme.txt", "no code here")
    with pytest.raises(WorkspaceError):
        find_entrypoint("e3")


# --- Running -----------------------------------------------------------------


def _run(session: str) -> list:
    """Collect all events from a run into a list."""
    return list(run_workspace(session))


def test_run_streams_command_output_and_done(workspace_root: Path) -> None:
    """A successful run yields a command, its output lines, then DONE."""
    _write(
        "r1",
        "main.py",
        """
        print("line one")
        print("line two")
        """,
    )
    events = _run("r1")

    assert events[0].kind is EventKind.COMMAND
    assert "main.py" in events[0].text
    outputs = [e.text for e in events if e.kind is EventKind.TOOL_RESULT]
    assert outputs == ["line one", "line two"]
    assert events[-1].kind is EventKind.DONE


def test_run_nonzero_exit_is_error(workspace_root: Path) -> None:
    """A script that exits non-zero ends with an ERROR event carrying the code."""
    _write(
        "r2",
        "main.py",
        """
        import sys
        print("about to fail")
        sys.exit(3)
        """,
    )
    events = _run("r2")
    assert events[-1].kind is EventKind.ERROR
    assert "3" in events[-1].text


def test_run_no_entrypoint_raises(workspace_root: Path) -> None:
    """Running a workspace with nothing runnable raises before any event."""
    get_or_create("r3")  # empty workspace
    with pytest.raises(WorkspaceError):
        _run("r3")


def test_run_timeout_is_reported(
    workspace_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run past the timeout is killed and reported as an error."""
    monkeypatch.setattr(get_settings(), "run_timeout_seconds", 1)
    _write(
        "r4",
        "main.py",
        """
        import time
        time.sleep(30)
        """,
    )
    events = _run("r4")
    assert events[-1].kind is EventKind.ERROR
    assert "timed out" in events[-1].text
