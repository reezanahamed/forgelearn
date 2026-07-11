"""In-memory session store — the persistence seam for the orchestrator (Phase 7).

The orchestrator keeps each learner's :class:`~forgelearn.common.types.Session`
(mission, ladder, progress, teach-backs) here, keyed by session id. Phase 7 holds
them in memory only: it is enough to prove the learning loop end-to-end for the
local single-user MVP, and it survives across HTTP requests within one server
run. Phase 8 replaces this class with a durable SQLite/JSON store *behind the same
interface* so callers (the engine, the routes) do not change.

Access goes through the process-wide singleton :func:`get_store`. A lock guards
the dict because FastAPI runs sync route handlers in a threadpool, so two
requests can touch the store concurrently.
"""

from __future__ import annotations

import threading

from forgelearn.common.errors import OrchestratorError
from forgelearn.common.logging import get_logger
from forgelearn.common.types import Session

_logger = get_logger("orchestrator.store")


class SessionStore:
    """A thread-safe, in-memory map of session id → :class:`Session`.

    The interface (``create``/``get``/``save``/``exists``) is what Phase 8's
    durable store must also implement, so swapping the backend is a one-line
    change in :func:`get_store`.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def create(self, session: Session) -> Session:
        """Store a new session.

        Args:
            session: The session to store; its id must be unused.

        Returns:
            The stored session (unchanged).

        Raises:
            OrchestratorError: If a session with the same id already exists.
        """
        with self._lock:
            if session.id in self._sessions:
                raise OrchestratorError(f"session already exists: {session.id!r}")
            self._sessions[session.id] = session
        _logger.debug("created session %s", session.id)
        return session

    def get(self, session_id: str) -> Session:
        """Return the stored session for ``session_id``.

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
            raise OrchestratorError(f"no such session: {session_id!r}")
        return session

    def save(self, session: Session) -> Session:
        """Persist changes to an existing session (upsert by id).

        Args:
            session: The session whose current state to store.

        Returns:
            The saved session (unchanged).
        """
        with self._lock:
            self._sessions[session.id] = session
        _logger.debug("saved session %s (stage=%s)", session.id, session.stage.value)
        return session

    def exists(self, session_id: str) -> bool:
        """Return whether a session with ``session_id`` is stored."""
        with self._lock:
            return session_id in self._sessions

    def list_sessions(self) -> list[Session]:
        """Return every stored session, newest first (for a resume picker).

        Returns:
            All sessions ordered by ``created_at`` descending.
        """
        with self._lock:
            sessions = list(self._sessions.values())
        return sorted(sessions, key=lambda s: s.created_at, reverse=True)

    def delete(self, session_id: str) -> None:
        """Remove a session if present (a no-op when it is not)."""
        with self._lock:
            self._sessions.pop(session_id, None)
        _logger.debug("deleted session %s", session_id)


_store: SessionStore | None = None
_store_lock = threading.Lock()


def get_store() -> SessionStore:
    """Return the process-wide session store singleton.

    Created lazily on first use. Phase 8 makes the default backend durable: a
    :class:`~forgelearn.storage.JsonSessionStore` (a :class:`SessionStore` subclass)
    that persists every session to disk under ``sessions_dir`` and reloads them on
    startup, so a learner resumes where they left off across restarts. The class
    is swapped here alone — every caller keeps the same interface.

    Returns:
        The shared session store.
    """
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                # Lazy import: keeps the store module free of a storage dependency
                # at import time and lets tests inject an in-memory store instead.
                from forgelearn.storage import JsonSessionStore

                _store = JsonSessionStore()
    return _store
