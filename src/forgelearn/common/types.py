"""Shared data models for ForgeLearn.

These Pydantic models mirror the product's data model (PLAN §6) and are the
common vocabulary passed between the orchestrator, storage, and server layers.
Defining them once here keeps the codebase DRY — no module redefines a Session
or Project shape of its own.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


def _utcnow() -> datetime:
    """Return a timezone-aware current UTC timestamp."""
    return datetime.now(timezone.utc)


class EventKind(str, Enum):
    """Semantic category of one thing the agent did (Phase 2).

    The raw provider stream (many JSON shapes) is normalized down to this small,
    provider-agnostic vocabulary so the browser and orchestrator can render and
    reason about agent activity without knowing which CLI produced it. The set
    is intentionally the four things a learner sees — the agent *talking*,
    *writing a file*, *running a command*, *finishing* — plus a few supporting
    kinds (other tool actions, tool output, setup, and failure).
    """

    SYSTEM = "system"  # session/setup lifecycle (init, retries) — usually hidden
    NARRATION = "narration"  # the agent's plain-text teaching voice
    FILE_WRITE = "file_write"  # the agent created or edited a file
    COMMAND = "command"  # the agent ran a shell command
    TOOL = "tool"  # any other tool action (read a file, search, …)
    TOOL_RESULT = "tool_result"  # output returned from a tool call
    DONE = "done"  # the run finished successfully
    ERROR = "error"  # the run finished (or a tool failed) with an error


class AgentEvent(BaseModel):
    """One clean, typed step in an agent run — the unit the UI renders.

    Produced by normalizing a provider's :class:`~forgelearn.agents.base.RawEvent`
    stream (see ``agents/events.py``). Every event is self-describing: ``kind``
    says what it is and ``text`` is a ready-to-display line, while the optional
    fields carry structure later phases need (which file, which tool, the
    session id) without re-parsing ``data``.

    Attributes:
        kind: The semantic category of this event.
        text: Human-readable, ready-to-print content or label for this event.
        tool: Provider tool name for ``FILE_WRITE`` / ``COMMAND`` / ``TOOL``.
        path: File path for a ``FILE_WRITE`` event, when known.
        is_error: True for an ``ERROR`` event or a failed ``TOOL_RESULT``.
        session_id: The agent session this event belongs to, when reported.
        data: Structured extras preserved for later phases (never required to
            render the event).
    """

    kind: EventKind
    text: str = ""
    tool: str | None = None
    path: str | None = None
    is_error: bool = False
    session_id: str | None = None
    data: dict = Field(default_factory=dict)

    @property
    def label(self) -> str:
        """A short uppercase tag for live, labeled printing (e.g. ``NARRATION``)."""
        return self.kind.value.upper()

    def pretty(self) -> str:
        """Render as one labeled line, e.g. ``[FILE_WRITE] hello.py``."""
        return f"[{self.label}] {self.text}".rstrip()


class ProjectStatus(str, Enum):
    """Lifecycle of a single project rung on the ladder."""

    LOCKED = "locked"
    ACTIVE = "active"
    BUILT = "built"
    COMPLETE = "complete"  # teach-back passed, next rung unlocked


class SessionStage(str, Enum):
    """Where a learner is in the method's state machine (Phase 7).

    The orchestrator advances a session through these stages; the browser reads
    the stage to decide which control to show (ask the topic, answer an interview
    question, build the active rung, or explain it in a teach-back).
    """

    NEW = "new"  # created, nothing captured yet
    INTERVIEW = "interview"  # questions generated, awaiting the learner's answers
    LADDER = "ladder"  # mission + ladder ready; the active rung awaits building
    BUILDING = "building"  # the agent is building the active rung right now
    TEACHBACK = "teachback"  # the rung is built; awaiting the learner's explanation
    COMPLETE = "complete"  # every rung passed its teach-back


class Project(BaseModel):
    """One rung on the learning ladder — a small thing the user builds.

    Attributes:
        id: Stable identifier, unique within a session's ladder.
        you_build: What the learner will build.
        you_learn: The concept the build teaches.
        done_when: The check that marks the build finished.
        status: Current lifecycle state.
    """

    id: str
    you_build: str
    you_learn: str
    done_when: str
    status: ProjectStatus = ProjectStatus.LOCKED


class ProgressEntry(BaseModel):
    """A dated progress line, compared only against day one (PLAN §6)."""

    on: date
    note: str


class TeachBack(BaseModel):
    """The user's explanation of a project plus the AI's probing verdict.

    Attributes:
        project_id: The rung the learner explained.
        explanation: The learner's explanation in their own words.
        probes: Weak-spot follow-up questions the judge raised (may spot-check an
            earlier concept for spacing/interleaving).
        feedback: The judge's short, encouraging assessment of the explanation.
        passed: True only when the explanation showed real mechanism, not
            restated words — the gate that unlocks the next rung.
    """

    project_id: str
    explanation: str
    probes: list[str] = Field(default_factory=list)
    feedback: str = ""
    passed: bool = False


class Session(BaseModel):
    """A single learner's run through the method (PLAN §6).

    Attributes:
        id: Unique session identifier.
        topic: The raw subject the learner typed ("I want to learn X").
        stage: Where the learner is in the method's state machine.
        interview_questions: The questions asked in the interview stage, awaiting
            (or already given) the learner's answers.
        mission: The learning goal in the user's own words, distilled from the
            interview.
        projects: The generated ladder of projects.
        progress: Dated progress log, day-one baseline first.
        teachbacks: Teach-back records, one per completed project.
        current_project_id: The rung the user is on, if any.
        workspace_path: Filesystem path to this session's project workspace.
        reading_grade: School grade level the AI writes for in this session.
        created_at: When the session was created (UTC).
    """

    id: str
    topic: str = ""
    stage: SessionStage = SessionStage.NEW
    interview_questions: list[str] = Field(default_factory=list)
    mission: str = ""
    projects: list[Project] = Field(default_factory=list)
    progress: list[ProgressEntry] = Field(default_factory=list)
    teachbacks: list[TeachBack] = Field(default_factory=list)
    current_project_id: str | None = None
    workspace_path: str | None = None
    reading_grade: int = 7
    created_at: datetime = Field(default_factory=_utcnow)

    def project(self, project_id: str) -> Project | None:
        """Return the rung with ``project_id``, or ``None`` if absent."""
        return next((p for p in self.projects if p.id == project_id), None)
