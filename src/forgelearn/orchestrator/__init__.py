"""The orchestrator — ForgeLearn's learning method (Phase 7, the core IP).

The state machine that turns the platform from a code builder into a *teacher*:
interview → generate a mission and a ladder of tiny projects → build each one
live while explaining → gate the next rung behind a teach-back → log progress
against day one. The teaching quality lives in :mod:`forgelearn.orchestrator.prompts`
(which implement ``TEACHING_PRINCIPLES.md``); :mod:`forgelearn.orchestrator.engine`
is the thin state machine; :mod:`forgelearn.orchestrator.store` is the persistence
seam Phase 8 will make durable.
"""

from __future__ import annotations

from forgelearn.orchestrator.engine import Orchestrator, TeachBackResult
from forgelearn.orchestrator.parsing import extract_json
from forgelearn.orchestrator.store import SessionStore, get_store

__all__ = [
    "Orchestrator",
    "TeachBackResult",
    "SessionStore",
    "get_store",
    "extract_json",
]
