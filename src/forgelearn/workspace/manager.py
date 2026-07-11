"""Locate, create, and inspect per-session project workspaces (Phase 5).

A workspace is a directory named ``session-<id>`` under the configured
``workspace_dir``. Its path is derived purely from the session id, so there is
no in-memory registry to keep in sync — the same id always maps to the same
folder, even across a server restart.

All filesystem access for a workspace goes through this module so path handling
lives in exactly one place. Two rules are enforced here to keep a browser-supplied
id or file path from escaping the workspace tree:

* a session id must match :data:`_SESSION_ID_RE` (no ``/``, ``..``, or dots), and
* a file path is resolved and must stay inside its workspace root.

Both violations raise :class:`~forgelearn.common.errors.WorkspaceError`.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from forgelearn.common.errors import WorkspaceError
from forgelearn.common.logging import get_logger
from forgelearn.config import get_settings

_logger = get_logger("workspace.manager")

# Folder name prefix for a session's workspace, e.g. ``session-a1b2c3d4``. The
# server, streams bridge, and tests all import this so the naming lives once.
SESSION_PREFIX = "session-"

# A session id is a browser-generated token (a UUID, hex, or similar). Restrict
# it to URL/path-safe characters with NO dot or slash so it can never contain a
# ``..`` traversal segment or an absolute path when joined onto workspace_dir.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Directories and files skipped when listing a workspace: build/runtime cruft the
# learner never authored and shouldn't see in the file tree.
_IGNORED_DIRS = frozenset({"__pycache__", ".git", ".mypy_cache", ".pytest_cache"})
_IGNORED_SUFFIXES = frozenset({".pyc", ".pyo"})

# Python source suffix — the only file type Phase 5 knows how to Run. Non-Python
# entrypoints (e.g. an HTML simulation) are a later concern (PLAN §3a).
_PY_SUFFIX = ".py"


@dataclass(frozen=True)
class FileEntry:
    """One file in a workspace, as shown in the browser file tree.

    Attributes:
        path: Path relative to the workspace root, using ``/`` separators.
        size: File size in bytes.
    """

    path: str
    size: int


def _validate_session_id(session_id: str) -> str:
    """Return ``session_id`` if it is a safe token, else raise.

    Args:
        session_id: The browser-supplied session identifier.

    Returns:
        The validated id, unchanged.

    Raises:
        WorkspaceError: If the id contains anything but ``[A-Za-z0-9_-]`` or is
            empty / too long (which could enable path traversal).
    """
    if not _SESSION_ID_RE.match(session_id):
        raise WorkspaceError(f"invalid session id: {session_id!r}")
    return session_id


def workspace_path(session_id: str) -> Path:
    """Return the workspace directory path for ``session_id`` (may not exist yet).

    Args:
        session_id: The session whose workspace path to compute.

    Returns:
        ``<workspace_dir>/session-<id>`` — no directory is created here.

    Raises:
        WorkspaceError: If the session id is not a safe token.
    """
    _validate_session_id(session_id)
    return get_settings().workspace_dir / f"{SESSION_PREFIX}{session_id}"


def get_or_create(session_id: str) -> Path:
    """Return the session's workspace directory, creating it if needed.

    Args:
        session_id: The session whose workspace to ensure.

    Returns:
        The existing workspace directory path.

    Raises:
        WorkspaceError: If the session id is invalid or the directory cannot be
            created.
    """
    path = workspace_path(session_id)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise WorkspaceError(f"could not create workspace {path}: {exc}") from exc
    _logger.debug("workspace ready: %s", path)
    return path


def create_ephemeral() -> Path:
    """Create a throwaway workspace with a generated id (no browser session).

    Used by the stream endpoint when a request carries no session id — the files
    still land in a real, isolated directory; they just aren't tracked for later
    listing or Run.

    Returns:
        The path to the newly created workspace directory.
    """
    return get_or_create(uuid.uuid4().hex[:8])


def _require_dir(session_id: str) -> Path:
    """Return an existing workspace dir for ``session_id`` or raise.

    Args:
        session_id: The session whose workspace must already exist.

    Returns:
        The workspace directory path.

    Raises:
        WorkspaceError: If the id is invalid or no workspace directory exists.
    """
    path = workspace_path(session_id)
    if not path.is_dir():
        raise WorkspaceError(f"no workspace for session {session_id!r}")
    return path


def _is_ignored(rel: Path) -> bool:
    """True if any path segment is an ignored dir or the suffix is ignored."""
    if rel.suffix in _IGNORED_SUFFIXES:
        return True
    return any(part in _IGNORED_DIRS for part in rel.parts)


def list_files(session_id: str) -> list[FileEntry]:
    """List the files in a session's workspace, newest-authored cruft excluded.

    Args:
        session_id: The session whose workspace to list.

    Returns:
        File entries with workspace-relative ``/``-separated paths, sorted
        alphabetically. Directories are implied by the paths; build/runtime
        artifacts (``__pycache__``, ``.pyc``, VCS dirs) are omitted. An empty or
        not-yet-created workspace yields an empty list.
    """
    root = workspace_path(session_id)
    if not root.is_dir():
        return []
    entries: list[FileEntry] = []
    for item in root.rglob("*"):
        if not item.is_file():
            continue
        rel = item.relative_to(root)
        if _is_ignored(rel):
            continue
        try:
            size = item.stat().st_size
        except OSError:  # pragma: no cover - file vanished mid-walk
            continue
        entries.append(FileEntry(path=rel.as_posix(), size=size))
    entries.sort(key=lambda e: e.path)
    return entries


def _resolve_within(root: Path, rel_path: str) -> Path:
    """Resolve ``rel_path`` against ``root`` and confirm it stays inside it.

    Args:
        root: The workspace root directory (need not be resolved yet).
        rel_path: A workspace-relative path from the browser.

    Returns:
        The resolved absolute path, guaranteed to be within ``root``.

    Raises:
        WorkspaceError: If the path escapes the workspace (traversal) or is
            absolute.
    """
    root_resolved = root.resolve()
    target = (root_resolved / rel_path).resolve()
    if target != root_resolved and root_resolved not in target.parents:
        raise WorkspaceError(f"path escapes workspace: {rel_path!r}")
    return target


def read_file(session_id: str, rel_path: str) -> str:
    """Read a workspace file's text for the browser file viewer.

    Args:
        session_id: The session that owns the file.
        rel_path: Workspace-relative path of the file to read.

    Returns:
        The file's text, decoded permissively (undecodable bytes replaced), and
        truncated to ``workspace_max_view_bytes`` with a marker if longer.

    Raises:
        WorkspaceError: If the session/path is invalid, the path escapes the
            workspace, or the target is not a readable file.
    """
    root = _require_dir(session_id)
    target = _resolve_within(root, rel_path)
    if not target.is_file():
        raise WorkspaceError(f"not a file: {rel_path!r}")

    cap = get_settings().workspace_max_view_bytes
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise WorkspaceError(f"could not read {rel_path!r}: {exc}") from exc

    truncated = raw[:cap]
    text = truncated.decode("utf-8", errors="replace")
    if len(raw) > cap:
        text += f"\n\n… truncated ({len(raw)} bytes; showing first {cap})"
    return text


def write_file(session_id: str, rel_path: str, content: str) -> None:
    """Write text to a workspace file, for the in-browser editor (redesign).

    Creates the session workspace and any parent directories as needed, so a
    learner can save a brand-new file. Path handling and traversal defence are the
    same as the read side.

    Args:
        session_id: The session that owns the file.
        rel_path: Workspace-relative path of the file to write.
        content: The text to write (UTF-8).

    Raises:
        WorkspaceError: If the session/path is invalid, the path escapes the
            workspace, the content is too large, or the write fails.
    """
    root = get_or_create(session_id)
    target = _resolve_within(root, rel_path)
    if target == root.resolve():
        raise WorkspaceError(f"not a file path: {rel_path!r}")
    cap = get_settings().workspace_max_edit_bytes
    if len(content.encode("utf-8")) > cap:
        raise WorkspaceError(f"file too large to save (max {cap} bytes)")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise WorkspaceError(f"could not write {rel_path!r}: {exc}") from exc
    _logger.debug("wrote %s to workspace %s", rel_path, session_id)


def workspace_summary(session_id: str) -> str:
    """Compose a text summary of a session's files for the AI to review.

    Lists each file and inlines its (truncated) text, so the build-review and hint
    prompts can read what the learner actually wrote. Binary/oversized files are
    noted, not dumped. An empty or missing workspace yields a short marker.

    Args:
        session_id: The session whose workspace to summarize.

    Returns:
        A plain-text summary: one ``### path`` heading and a fenced body per file.
    """
    entries = list_files(session_id)
    if not entries:
        return "(the learner has not created any files yet)"
    parts: list[str] = []
    for entry in entries:
        try:
            text = read_file(session_id, entry.path)
        except WorkspaceError:
            parts.append(f"### {entry.path}\n(could not read this file)")
            continue
        parts.append(f"### {entry.path}\n```\n{text}\n```")
    return "\n\n".join(parts)


def read_bytes(session_id: str, rel_path: str) -> bytes:
    """Read a workspace file's full, untruncated bytes.

    Unlike :func:`read_file` (which decodes and caps output for the browser
    viewer), this returns the exact bytes on disk — used by the exporter to inline
    a file's real content (as text or a ``data:`` URI) into a self-contained HTML.

    Args:
        session_id: The session that owns the file.
        rel_path: Workspace-relative path of the file to read.

    Returns:
        The file's raw bytes.

    Raises:
        WorkspaceError: If the session/path is invalid, the path escapes the
            workspace, or the target is not a readable file.
    """
    root = _require_dir(session_id)
    target = _resolve_within(root, rel_path)
    if not target.is_file():
        raise WorkspaceError(f"not a file: {rel_path!r}")
    try:
        return target.read_bytes()
    except OSError as exc:
        raise WorkspaceError(f"could not read {rel_path!r}: {exc}") from exc


def find_entrypoint(session_id: str) -> Path:
    """Resolve which file a Run should execute for this workspace.

    Selection order: a configured priority name (``run_entrypoints``, e.g.
    ``main.py``) if present; otherwise the sole ``.py`` file; otherwise the
    most recently modified ``.py`` file. Python is the only runnable type in
    Phase 5.

    Args:
        session_id: The session whose project to run.

    Returns:
        The absolute path of the file to execute.

    Raises:
        WorkspaceError: If the workspace is missing or holds no ``.py`` file.
    """
    root = _require_dir(session_id)
    py_files = [
        p
        for p in root.rglob(f"*{_PY_SUFFIX}")
        if p.is_file() and not _is_ignored(p.relative_to(root))
    ]
    if not py_files:
        raise WorkspaceError(
            f"nothing to run: no {_PY_SUFFIX} file in session {session_id!r}"
        )

    # 1) A configured priority name, matched at the workspace root.
    for name in _configured_entrypoints():
        candidate = root / name
        if candidate in py_files:
            _logger.debug("entrypoint by priority name: %s", name)
            return candidate

    # 2) The sole Python file, if there's exactly one.
    if len(py_files) == 1:
        return py_files[0]

    # 3) Fall back to the most recently modified Python file.
    newest = max(py_files, key=lambda p: p.stat().st_mtime)
    _logger.debug("entrypoint by newest mtime: %s", newest.name)
    return newest


def _configured_entrypoints() -> list[str]:
    """Parse the ``run_entrypoints`` setting into an ordered filename list."""
    raw = get_settings().run_entrypoints.replace(",", " ")
    return [name for name in raw.split() if name]
