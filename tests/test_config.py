"""Smoke tests for central config, shared logging, errors, and types."""

from __future__ import annotations

import logging
from pathlib import Path

from forgelearn.common.errors import ConfigError, ForgeLearnError
from forgelearn.common.logging import get_logger
from forgelearn.common.types import Project, ProjectStatus, Session
from forgelearn.config import PLATFORM_ROOT, Settings, get_settings


def test_settings_have_expected_defaults() -> None:
    """Defaults from the spec (port 8000, agent 'claude') load correctly."""
    settings = Settings()
    assert settings.port == 8000
    assert settings.host == "127.0.0.1"
    assert settings.default_agent == "claude"
    assert settings.log_level == "INFO"


def test_relative_storage_paths_anchor_to_root() -> None:
    """Relative workspace/session dirs resolve under the platform root."""
    settings = Settings()
    assert settings.workspace_dir.is_absolute()
    assert settings.sessions_dir.is_absolute()
    assert settings.workspace_dir == PLATFORM_ROOT / "workspaces"


def test_absolute_storage_path_is_kept(tmp_path: Path) -> None:
    """An absolute override is honored verbatim, not re-anchored."""
    settings = Settings(workspace_dir=tmp_path)
    assert settings.workspace_dir == tmp_path


def test_log_level_is_normalized() -> None:
    """Lowercase log levels are uppercased for the stdlib logger."""
    assert Settings(log_level="debug").log_level == "DEBUG"


def test_interview_is_an_adaptive_range() -> None:
    """The interview is a bounded range (not a fixed count), default 3–6."""
    settings = Settings()
    assert settings.interview_min_questions == 3
    assert settings.interview_max_questions == 6


def test_inverted_bounds_are_corrected() -> None:
    """A max below its min is clamped up, so a range can never be invalid."""
    settings = Settings(interview_max_questions=1, ladder_max_projects=2)
    assert settings.interview_max_questions == settings.interview_min_questions
    assert settings.ladder_max_projects == settings.ladder_min_projects


def test_get_settings_is_cached() -> None:
    """The settings accessor returns a shared singleton."""
    assert get_settings() is get_settings()


def test_get_logger_returns_namespaced_logger() -> None:
    """The shared logger lives under the 'forgelearn' namespace."""
    logger = get_logger("cli")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "forgelearn.cli"


def test_error_hierarchy() -> None:
    """Custom errors derive from the ForgeLearn base error."""
    assert issubclass(ConfigError, ForgeLearnError)
    assert issubclass(ForgeLearnError, Exception)


def test_session_and_project_models() -> None:
    """Core data models construct with sensible defaults."""
    project = Project(
        id="p1",
        you_build="a dice roller",
        you_learn="random numbers",
        done_when="it prints 1-6",
    )
    assert project.status is ProjectStatus.LOCKED

    session = Session(id="s1", mission="learn python")
    assert session.projects == []
    assert session.created_at.tzinfo is not None
