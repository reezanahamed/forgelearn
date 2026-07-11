"""Agent adapters — ForgeLearn's swappable, headless CLI engine (PLAN §5).

One :class:`~forgelearn.agents.base.AgentAdapter` interface, many providers.
Phase 1 shipped a single provider (Claude Code); Phase 6 adds a second (OpenAI
Codex) behind the same interface and drives selection through the :func:`get_agent`
factory — the seam the browser dropdown routes through, without touching callers.
Registering a further provider is one line in ``_ADAPTERS`` below.
"""

from __future__ import annotations

from forgelearn.agents.base import AgentAdapter, RawEvent
from forgelearn.agents.claude import ClaudeAgent
from forgelearn.agents.codex import CodexAgent
from forgelearn.agents.events import stream_events, to_events
from forgelearn.common.errors import AgentError
from forgelearn.common.types import AgentEvent, EventKind

# Registry of known providers, keyed by the name used in config / the dropdown.
# The dropdown lists exactly these keys; adding a provider here surfaces it.
_ADAPTERS: dict[str, type[AgentAdapter]] = {
    ClaudeAgent.name: ClaudeAgent,
    CodexAgent.name: CodexAgent,
}


def available_agents() -> list[str]:
    """Return the sorted names of all registered agent providers."""
    return sorted(_ADAPTERS)


def get_agent(name: str) -> AgentAdapter:
    """Instantiate the agent adapter registered under ``name``.

    Args:
        name: Provider identifier (e.g. ``"claude"``), matching a dropdown value.

    Returns:
        A ready-to-use :class:`AgentAdapter`.

    Raises:
        AgentError: If no provider is registered under ``name``.
    """
    try:
        adapter_cls = _ADAPTERS[name]
    except KeyError:
        raise AgentError(
            f"unknown agent {name!r}; available: {', '.join(available_agents())}"
        ) from None
    return adapter_cls()


__all__ = [
    "AgentAdapter",
    "RawEvent",
    "AgentEvent",
    "EventKind",
    "ClaudeAgent",
    "CodexAgent",
    "get_agent",
    "available_agents",
    "stream_events",
    "to_events",
]
