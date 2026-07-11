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
    SYLLABUS = "syllabus"  # (redesign) course syllabus generated; lessons await
    LEARNING = "learning"  # (redesign) the learner is inside a lesson
    COMPLETE = "complete"  # every rung passed its teach-back


class LessonStage(str, Enum):
    """Where the learner is inside a single lesson (the redesign's inner loop).

    Each lesson walks through: read the concept and play the interactive widget,
    answer a quick check, watch the AI build a worked example, then build your own
    version and get it reviewed. The browser reads this to show the right panel.
    """

    LEARN = "learn"  # reading the concept + playing the interactive widget
    CHECK = "check"  # answering the understanding question, awaiting the verdict
    DEMO = "demo"  # the AI is building the worked example to watch
    BUILD = "build"  # the learner builds their own version, awaiting review
    DONE = "done"  # lesson complete, next unlocked


class Widget(BaseModel):
    """A self-contained interactive element the learner plays with (the redesign).

    Brilliant-style: a slider, drag-and-drop, or canvas manipulative the AI
    generates as one self-contained HTML document (inline CSS/JS, no external
    requests). The browser renders it in a sandboxed iframe. ``html`` stays empty
    until the lesson content is generated.

    Attributes:
        title: A short label shown above the widget.
        html: A complete, self-contained HTML document for the manipulative.
        caption: One line telling the learner what to try.
    """

    title: str = ""
    html: str = ""
    caption: str = ""


class Check(BaseModel):
    """A quick understanding question inside a lesson (the redesign).

    Graded by the AI at answer time (no stored answer key), so it works for both
    multiple-choice and short free-text answers.

    Attributes:
        question: The question text.
        kind: ``"mcq"`` or ``"short"``.
        options: Answer options for a multiple-choice question.
    """

    question: str = ""
    kind: str = "short"
    options: list[str] = Field(default_factory=list)


class Lesson(BaseModel):
    """One lesson in a course (the redesign's rung).

    A lesson teaches a single concept, then has the learner build it. It is filled
    in lazily: the syllabus creates it with a title and the two build tasks; the
    concept/widget/check are generated when the learner opens it.

    Attributes:
        id: Stable identifier, unique within the course.
        title: Short lesson title.
        goal: One line on what the learner will be able to do after it.
        domain_type: ``"code"`` (real code in an editor) or ``"interactive"``
            (a browser simulation/visual) — chosen by subject (PLAN §3a).
        concept: Plain-English explanation with a concrete example (generated).
        widget: The interactive manipulative (generated).
        check: The understanding question (generated).
        demo_task: What the AI builds as the worked example.
        build_task: What the learner then builds themselves.
        status: Lifecycle across the course (locked/active/complete).
        stage: Where the learner is within this lesson.
    """

    id: str
    title: str
    goal: str = ""
    domain_type: str = "code"
    concept: str = ""
    widget: Widget | None = None
    check: Check | None = None
    demo_task: str = ""
    build_task: str = ""
    status: ProjectStatus = ProjectStatus.LOCKED
    stage: LessonStage = LessonStage.LEARN


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
    # --- Redesign (guided lessons): the course of lessons and the active one ---
    lessons: list["Lesson"] = Field(default_factory=list)
    active_lesson_id: str | None = None
    created_at: datetime = Field(default_factory=_utcnow)

    def project(self, project_id: str) -> Project | None:
        """Return the rung with ``project_id``, or ``None`` if absent."""
        return next((p for p in self.projects if p.id == project_id), None)

    def lesson(self, lesson_id: str) -> "Lesson | None":
        """Return the lesson with ``lesson_id``, or ``None`` if absent."""
        return next((les for les in self.lessons if les.id == lesson_id), None)
