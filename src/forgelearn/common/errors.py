"""Custom exception hierarchy for ForgeLearn.

Library code raises these instead of bare ``Exception`` so that boundaries
(server routes, the CLI) can catch, log, and translate them consistently.
Every ForgeLearn-specific error derives from :class:`ForgeLearnError`.
"""

from __future__ import annotations


class ForgeLearnError(Exception):
    """Base class for all ForgeLearn errors."""


class ConfigError(ForgeLearnError):
    """Raised when configuration is missing or invalid."""


class AgentError(ForgeLearnError):
    """Raised when a headless CLI coding agent fails to run or produce output."""


class WorkspaceError(ForgeLearnError):
    """Raised for per-session workspace filesystem problems."""


class StorageError(ForgeLearnError):
    """Raised when persisting or loading session state fails."""


class OrchestratorError(ForgeLearnError):
    """Raised when the learning-method state machine reaches an invalid state."""
