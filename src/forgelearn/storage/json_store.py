"""Durable, file-backed session store (Phase 8).

This is the persistence Phase 7 deferred: the same
:class:`~forgelearn.orchestrator.store.SessionStore` interface (``create`` /
``get`` / ``save`` / ``exists`` / ``list_sessions``), but every session is
written to disk as one JSON file under the configured ``sessions_dir`` and
reloaded on startup. Because it subclasses the in-memory store, callers — the
engine, the routes — are unchanged; :func:`~forgelearn.orchestrator.store.get_store`
just constructs this instead.

Each session lives at ``<sessions_dir>/<id>.json`` (the id is a server-issued
UUID hex, validated before it is turned into a filename so a stray value can't
escape the directory). The in-memory dict from the base class is kept as a
write-through cache: reads are served from memory, and every ``create``/``save``
also writes the file so the state survives a restart and a returning learner
resumes exactly where they left off.
"""

from __future__ import annotations

import re
from pathlib import Path

from forgelearn.common.errors import OrchestratorError, StorageError
from forgelearn.common.logging import get_logger
from forgelearn.common.types import Session
from forgelearn.config import get_settings
from forgelearn.orchestrator.store import SessionStore

_logger = get_logger("storage.json_store")

# Extension for one persisted session document.
_SUFFIX = ".json"

# A session id becomes a filename, so restrict it to path-safe characters with no
# dot or slash — the same guarantee the workspace layer makes for its folders.
# Server-issued ids are UUID hex, which always satisfies this; the guard defends
# against a hand-crafted id arriving via a resume/fetch request.
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class JsonSessionStore(SessionStore):
    """A :class:`SessionStore` that persists each session to a JSON file.

    The base class's ``_sessions`` dict and ``_lock`` are reused as a
    thread-safe, write-through cache in front of the files on disk.
    """

    def __init__(self, directory: Path | None = None) -> None:
        """Open (creating if needed) the directory and load existing sessions.

        Args:
            directory: Where session files live. Defaults to the configured
                ``sessions_dir``.

        Raises:
            StorageError: If the directory cannot be created.
        """
        super().__init__()
        self._dir = directory or get_settings().sessions_dir
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(f"could not open sessions dir {self._dir}: {exc}") from exc
        self._load_all()

    # --- Interface (write-through over the base class's cache) ---------------

    def create(self, session: Session) -> Session:
        """Store a new session in memory and on disk.

        Args:
            session: The session to store; its id must be unused.

        Returns:
            The stored session (unchanged).

        Raises:
            OrchestratorError: If a session with the same id already exists.
            StorageError: If the file cannot be written.
        """
        with self._lock:
            if session.id in self._sessions or self._path_for(session.id).exists():
                raise OrchestratorError(f"session already exists: {session.id!r}")
            self._sessions[session.id] = session
            self._write(session)
        _logger.debug("created + persisted session %s", session.id)
        return session

    def get(self, session_id: str) -> Session:
        """Return a session from the cache, falling back to disk.

        Args:
            session_id: The session to fetch.

        Returns:
            The stored :class:`Session`.

        Raises:
            OrchestratorError: If no such session exists.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                session = self._load_one(session_id)  # a file written out-of-band
                if session is not None:
                    self._sessions[session_id] = session
        if session is None:
            raise OrchestratorError(f"no such session: {session_id!r}")
        return session

    def save(self, session: Session) -> Session:
        """Persist the current state of a session (upsert by id).

        Args:
            session: The session whose state to store.

        Returns:
            The saved session (unchanged).

        Raises:
            StorageError: If the file cannot be written.
        """
        with self._lock:
            self._sessions[session.id] = session
            self._write(session)
        _logger.debug("saved session %s (stage=%s)", session.id, session.stage.value)
        return session

    def exists(self, session_id: str) -> bool:
        """Return whether a session is cached or persisted on disk."""
        with self._lock:
            if session_id in self._sessions:
                return True
            try:
                return self._path_for(session_id).exists()
            except StorageError:
                return False

    # --- Disk I/O helpers ----------------------------------------------------

    def _path_for(self, session_id: str) -> Path:
        """Return the file path for ``session_id``, validating it is path-safe.

        Raises:
            StorageError: If the id is not a safe token (could escape the dir).
        """
        if not _SESSION_ID_RE.match(session_id):
            raise StorageError(f"invalid session id: {session_id!r}")
        return self._dir / f"{session_id}{_SUFFIX}"

    def _write(self, session: Session) -> None:
        """Write one session to its JSON file (caller holds the lock).

        Raises:
            StorageError: If serialization or the file write fails.
        """
        path = self._path_for(session.id)
        try:
            path.write_text(session.model_dump_json(), encoding="utf-8")
        except OSError as exc:
            raise StorageError(f"could not write session {session.id!r}: {exc}") from exc

    def _load_one(self, session_id: str) -> Session | None:
        """Load a single session from disk, or None if its file is absent."""
        try:
            path = self._path_for(session_id)
        except StorageError:
            return None
        if not path.is_file():
            return None
        return _read_session_file(path)

    def _load_all(self) -> None:
        """Populate the cache from every valid session file in the directory.

        A corrupt or unreadable file is logged and skipped rather than aborting
        startup — one bad session must not lock the learner out of the others.
        """
        for path in sorted(self._dir.glob(f"*{_SUFFIX}")):
            session = _read_session_file(path)
            if session is not None:
                self._sessions[session.id] = session
        _logger.info("loaded %d session(s) from %s", len(self._sessions), self._dir)


def _read_session_file(path: Path) -> Session | None:
    """Parse one session file, returning None (and logging) on any failure."""
    try:
        return Session.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:  # ValueError covers pydantic validation
        _logger.warning("skipping unreadable session file %s: %s", path.name, exc)
        return None
