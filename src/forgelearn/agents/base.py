"""The agent-adapter interface — ForgeLearn's swappable engine boundary.

Every provider (Claude Code, Codex, Grok, …) is driven through the single
:class:`AgentAdapter` interface so the rest of the platform never knows which
CLI is running underneath (PLAN §5). An adapter takes a natural-language prompt
plus a workspace directory and yields an ordered stream of :class:`RawEvent`
objects — one per line the CLI emits.

Phase 1 deliberately yields *raw* events (the provider's own JSON, lightly
wrapped). Turning these into clean, semantic event types (narration vs
file-write vs command vs done) is Phase 2's job; keeping the split here means
each adapter stays a thin transport and the normalization lives in one place.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # avoid a base<->events import cycle at module load time
    from forgelearn.common.types import AgentEvent

# Upper bound on characters logged/echoed from a prompt, so a long instruction
# never floods a log line.
_PROMPT_LOG_CHARS = 120


@dataclass(frozen=True)
class RawEvent:
    """One event emitted by a headless agent CLI.

    A provider-agnostic wrapper around a single line of the agent's streamed
    output. The ``type``/``subtype`` pair is lifted to the top level for easy
    dispatch, while ``data`` preserves the full original payload so later
    phases can extract whatever they need without re-running the agent.

    Attributes:
        type: The event's ``type`` field (e.g. ``"assistant"``, ``"result"``).
        subtype: The event's ``subtype`` field if present (e.g. ``"init"``,
            ``"success"``), else ``None``.
        data: The complete decoded JSON object for this event.
    """

    type: str
    subtype: str | None = None
    data: dict = field(default_factory=dict)


class AgentAdapter(ABC):
    """Abstract driver for one headless CLI coding agent.

    Concrete adapters implement :meth:`run`. Each adapter advertises the
    provider ``name`` used by the browser dropdown / factory (Phase 6).
    """

    #: Stable provider identifier (matches the dropdown value and the factory key).
    name: str = "base"

    @abstractmethod
    def run(self, prompt: str, workspace: Path) -> Iterator[RawEvent]:
        """Run the agent on ``prompt`` inside ``workspace`` and stream events.

        Args:
            prompt: The natural-language instruction for the agent.
            workspace: An existing directory the agent runs in; any files it
                creates land here.

        Yields:
            :class:`RawEvent` objects in the order the CLI emits them.

        Raises:
            WorkspaceError: If ``workspace`` is missing or not a directory.
            AgentError: If the agent process fails, times out, or reports an
                error result.
        """
        raise NotImplementedError

    def run_events(self, prompt: str, workspace: Path) -> Iterator["AgentEvent"]:
        """Run the agent and stream clean, semantic events (Phase 2).

        A thin convenience over :meth:`run`: it pipes the raw provider stream
        through the shared normalizer so callers (the server, the orchestrator)
        get provider-agnostic :class:`~forgelearn.common.types.AgentEvent` objects
        instead of raw JSON. Providers whose raw schema differs from the
        Anthropic ``stream-json`` shape override this method.

        Args:
            prompt: The natural-language instruction for the agent.
            workspace: An existing directory to run inside.

        Yields:
            :class:`~forgelearn.common.types.AgentEvent` objects in order.

        Raises:
            WorkspaceError: If ``workspace`` is missing or not a directory.
            AgentError: If the agent process fails, times out, or reports an
                error result.
        """
        # Local import breaks the base<->events module cycle (events needs
        # RawEvent from this module).
        from forgelearn.agents.events import stream_events

        yield from stream_events(self.run(prompt, workspace))

    def complete(self, prompt: str, workspace: Path) -> str:
        """Run the agent for a single text answer and return it as one string.

        The build stream (:meth:`run_events`) is for *watching* an agent work;
        this is for *asking* it a question. The orchestrator (Phase 7) uses it to
        get plain-text or JSON answers — interview questions, a ladder, a
        teach-back verdict — from the same swappable engine that builds projects,
        so ForgeLearn never needs a second, direct LLM API path.

        The agent's plain-text narration is collected and returned; the terminal
        summary is used as a fallback if no narration was emitted. Prompts that
        want structured output should instruct the agent to reply with JSON only.

        Args:
            prompt: The question to ask the agent.
            workspace: An existing directory the agent may run in (a scratch dir
                is fine — a completion is not expected to write files).

        Returns:
            The agent's answer as a single trimmed string.

        Raises:
            WorkspaceError: If ``workspace`` is missing or not a directory.
            AgentError: If the agent process fails, times out, reports an error
                result, or returns no text at all.
        """
        # Local import mirrors run_events: avoid importing the types/events layer
        # at module load so the base stays a thin interface.
        from forgelearn.common.errors import AgentError
        from forgelearn.common.types import EventKind

        parts: list[str] = []
        fallback = ""
        for event in self.run_events(prompt, workspace):
            if event.kind is EventKind.NARRATION and event.text:
                parts.append(event.text)
            elif event.kind is EventKind.DONE and event.text:
                fallback = event.text
            elif event.kind is EventKind.ERROR:
                raise AgentError(event.text or "agent completion failed")
        answer = "\n".join(parts).strip() or fallback.strip()
        if not answer:
            raise AgentError(
                f"agent {self.name!r} returned no text for prompt "
                f"{prompt[:_PROMPT_LOG_CHARS]!r}"
            )
        return answer
