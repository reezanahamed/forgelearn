"""Offline tests for durable storage and HTML export (Phase 8).

These exercise the two Phase 8 pieces directly, no server:

* :class:`~forgelearn.storage.JsonSessionStore` — the durable, file-backed drop-in
  for the in-memory store: create/get/save round-trip, resume from disk in a
  fresh instance, duplicate/unknown handling, listing, id safety, and tolerance
  of a corrupt file.
* :func:`~forgelearn.storage.export_session_html` — a self-contained HTML export
  that inlines the session record and every built workspace file (text in
  ``<pre>``, images as ``data:`` URIs), with all text escaped.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from forgelearn.common.errors import OrchestratorError, StorageError
from forgelearn.common.types import (
    ProgressEntry,
    Project,
    ProjectStatus,
    Session,
    SessionStage,
    TeachBack,
)
from forgelearn.config import get_settings
from forgelearn.storage import JsonSessionStore, export_session_html
from forgelearn.storage.export import _data_uri
from forgelearn.workspace import get_or_create


def _session(session_id: str = "abc123", **overrides) -> Session:
    """A minimal but populated session for round-trip and export tests."""
    base = dict(
        id=session_id,
        topic="reinforcement learning",
        stage=SessionStage.COMPLETE,
        mission="Learn RL to build a trading agent",
        projects=[
            Project(
                id="p1",
                you_build="a bandit",
                you_learn="exploration vs exploitation",
                done_when="beats random",
                status=ProjectStatus.COMPLETE,
            )
        ],
        progress=[ProgressEntry(on=date(2026, 7, 10), note="Day one — starting fresh")],
        teachbacks=[
            TeachBack(project_id="p1", explanation="epsilon balances it", feedback="Good.", passed=True)
        ],
        created_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return Session(**base)


# --- JsonSessionStore --------------------------------------------------------


def test_create_persists_and_resumes_in_a_fresh_store(tmp_path: Path) -> None:
    """A created session survives — a brand-new store instance loads it back."""
    JsonSessionStore(tmp_path).create(_session())

    # A separate instance (as if after a server restart) sees it on disk.
    resumed = JsonSessionStore(tmp_path).get("abc123")
    assert resumed.mission.startswith("Learn RL")
    assert resumed.stage is SessionStage.COMPLETE
    assert resumed.projects[0].status is ProjectStatus.COMPLETE
    assert (tmp_path / "abc123.json").is_file()


def test_save_updates_the_persisted_state(tmp_path: Path) -> None:
    """Saving writes through to disk so the change is durable."""
    store = JsonSessionStore(tmp_path)
    session = store.create(_session(stage=SessionStage.LADDER))
    session.stage = SessionStage.COMPLETE
    store.save(session)

    assert JsonSessionStore(tmp_path).get("abc123").stage is SessionStage.COMPLETE


def test_duplicate_create_is_rejected(tmp_path: Path) -> None:
    """Creating the same id twice fails (same contract as the in-memory store)."""
    store = JsonSessionStore(tmp_path)
    store.create(_session())
    with pytest.raises(OrchestratorError):
        store.create(_session())


def test_get_unknown_session_raises(tmp_path: Path) -> None:
    """Fetching a missing session is a clean error, not a crash."""
    with pytest.raises(OrchestratorError):
        JsonSessionStore(tmp_path).get("missing")


def test_exists_reflects_cache_and_disk(tmp_path: Path) -> None:
    """exists() is true for a stored session and false otherwise."""
    store = JsonSessionStore(tmp_path)
    assert store.exists("abc123") is False
    store.create(_session())
    assert store.exists("abc123") is True


def test_list_sessions_is_newest_first(tmp_path: Path) -> None:
    """Sessions are listed newest-first for a resume picker."""
    store = JsonSessionStore(tmp_path)
    store.create(_session("old", created_at=datetime(2026, 7, 1, tzinfo=timezone.utc)))
    store.create(_session("new", created_at=datetime(2026, 7, 10, tzinfo=timezone.utc)))
    assert [s.id for s in store.list_sessions()] == ["new", "old"]


def test_unsafe_session_id_is_rejected(tmp_path: Path) -> None:
    """An id that could escape the directory never becomes a file path."""
    store = JsonSessionStore(tmp_path)
    with pytest.raises(StorageError):
        store.create(_session("../escape"))


def test_corrupt_file_is_skipped_not_fatal(tmp_path: Path) -> None:
    """A garbage session file is ignored on load; the good ones still load."""
    JsonSessionStore(tmp_path).create(_session("good"))
    (tmp_path / "broken.json").write_text("{not valid json", encoding="utf-8")

    store = JsonSessionStore(tmp_path)  # must not raise
    assert [s.id for s in store.list_sessions()] == ["good"]


# --- HTML export -------------------------------------------------------------


@pytest.fixture()
def workspaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point per-session workspaces at a tmp dir (restored after the test)."""
    monkeypatch.setattr(get_settings(), "workspace_dir", tmp_path)
    return tmp_path


def test_export_is_self_contained_and_includes_the_record(workspaces: Path) -> None:
    """The export is one standalone HTML holding the mission/ladder/progress."""
    html = export_session_html(_session())

    assert html.startswith("<!doctype html>")
    assert "Learn RL to build a trading agent" in html
    assert "a bandit" in html  # a ladder rung
    assert "Day one" in html  # progress vs day one
    assert "epsilon balances it" in html  # the teach-back
    # Self-contained: no external asset requests.
    assert "http://" not in html and "https://" not in html
    assert "src=\"http" not in html


def test_export_inlines_workspace_files(workspaces: Path) -> None:
    """A built file's real content is embedded, HTML-escaped, in the page."""
    ws = get_or_create("abc123")
    (ws / "main.py").write_text("print(1 < 2)\n", encoding="utf-8")

    html = export_session_html(_session())
    assert "main.py" in html
    # The angle bracket in the source is escaped, never emitted raw.
    assert "print(1 &lt; 2)" in html


def test_export_inlines_images_as_data_uris(workspaces: Path) -> None:
    """A binary image asset is inlined as a base64 data: URI, not a file link."""
    ws = get_or_create("abc123")
    # A 1x1 PNG (smallest valid-ish bytes are fine; export doesn't decode it).
    (ws / "plot.png").write_bytes(b"\x89PNG\r\n\x1a\nfake-image-bytes")

    html = export_session_html(_session())
    assert "data:image/png;base64," in html


def test_data_uri_roundtrips_bytes() -> None:
    """The data: URI helper base64-encodes the given bytes under the given MIME."""
    uri = _data_uri("image/png", b"abc")
    assert uri == "data:image/png;base64,YWJj"
