"""Durable session persistence and project export (Phase 8).

Phase 7 proved the learning loop with an in-memory store; Phase 8 makes it last:

* :mod:`forgelearn.storage.json_store` — :class:`JsonSessionStore`, a durable,
  file-backed drop-in for the in-memory
  :class:`~forgelearn.orchestrator.store.SessionStore`, so a learner resumes where
  they left off across restarts. :func:`~forgelearn.orchestrator.store.get_store`
  constructs it; no caller changes.
* :mod:`forgelearn.storage.export` — :func:`export_session_html`, which turns a
  finished session (mission, ladder, progress, teach-backs, and the built files)
  into one self-contained, offline HTML document with every asset inlined.
"""

from __future__ import annotations

from forgelearn.storage.export import export_session_html
from forgelearn.storage.json_store import JsonSessionStore

__all__ = ["JsonSessionStore", "export_session_html"]
