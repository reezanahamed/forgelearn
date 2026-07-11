"""The orchestrator — ForgeLearn's learning method as a state machine (Phase 7).

This is the product's core value. It advances a learner through the method:

    interview  →  ladder  →  build  →  teach-back gate  →  (unlock next) …

Each step asks the swappable engine (the same CLI coding agent that builds
projects) a question via :meth:`~forgelearn.agents.base.AgentAdapter.complete`,
using the prompts in :mod:`forgelearn.orchestrator.prompts` (which carry the
teaching principles), parses the structured reply, and moves the stored
:class:`~forgelearn.common.types.Session` forward. The class stays deliberately
thin: the *teaching* lives in the prompts, the *transport* in the agents layer,
and *persistence* behind the :class:`~forgelearn.orchestrator.store.SessionStore`
seam — this file only orchestrates.

Building a project is not driven here: it is a live agent stream the browser
watches (Phase 3/5). The orchestrator supplies the build *prompt*
(:meth:`build_instruction`) and records the outcome (:meth:`mark_built`); the
server bridge runs the stream.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from forgelearn.agents import get_agent
from forgelearn.agents.base import AgentAdapter
from forgelearn.common.errors import OrchestratorError
from forgelearn.common.logging import get_logger
from forgelearn.common.types import (
    ProgressEntry,
    Project,
    ProjectStatus,
    Session,
    SessionStage,
    TeachBack,
)
from forgelearn.config import get_settings
from forgelearn.orchestrator.parsing import extract_json
from forgelearn.orchestrator.prompts import (
    build_prompt,
    interview_prompt,
    mission_and_ladder_prompt,
    teachback_prompt,
)
from forgelearn.orchestrator.store import SessionStore, get_store
from forgelearn.workspace import create_ephemeral

_logger = get_logger("orchestrator.engine")

# Guardrails on learner-supplied free text, so a stray huge paste can't be sent
# to the engine as a prompt. Generous — these are ceilings, not limits on real use.
_MAX_TOPIC_CHARS = 500
_MAX_ANSWER_CHARS = 5_000
_MAX_EXPLANATION_CHARS = 20_000


@dataclass
class TeachBackResult:
    """The outcome of judging one teach-back (what the browser needs to react).

    Attributes:
        teachback: The stored teach-back record (explanation, probes, verdict).
        passed: Whether the gate opened.
        progress_note: The day-one-relative progress line the judge wrote.
        storage_note: A tip for making the learning durable (not persisted).
        next_project_id: The rung unlocked by passing, if any.
        stage: The session's stage after judging.
    """

    teachback: TeachBack
    passed: bool
    progress_note: str = ""
    storage_note: str = ""
    next_project_id: str | None = None
    stage: SessionStage = SessionStage.TEACHBACK


class Orchestrator:
    """Drives one learner through the interview → ladder → build → teach-back loop.

    The engine and store are injectable so tests can drive the whole method with
    a scripted fake agent and no subprocess or global state.
    """

    def __init__(
        self, agent: AgentAdapter | None = None, store: SessionStore | None = None
    ) -> None:
        """Build an orchestrator.

        Args:
            agent: The engine used for the orchestrator's own questions. When
                ``None``, the configured ``orchestrator_agent`` (else
                ``default_agent``) is resolved per call.
            store: The session store. When ``None``, the process-wide singleton
                is used.
        """
        self._agent = agent
        self._store = store or get_store()

    # --- Stage 1: interview --------------------------------------------------

    def start(self, topic: str, grade: int | None = None) -> Session:
        """Begin a session: capture the topic and generate interview questions.

        Args:
            topic: The subject the learner wants to learn ("I want to learn X").
            grade: School grade level to write for; ``None`` uses the configured
                default. Stored on the session and reused by later stages.

        Returns:
            The new session, stage ``INTERVIEW``, with its questions populated.

        Raises:
            OrchestratorError: If the topic is empty/too long or the engine's
                reply cannot be parsed into questions.
        """
        topic = _clean(topic, _MAX_TOPIC_CHARS, "topic")
        settings = get_settings()
        grade = _clamp_grade(grade if grade is not None else settings.reading_grade_default)
        prompt = interview_prompt(
            topic,
            settings.interview_min_questions,
            settings.interview_max_questions,
            grade,
        )
        data = self._ask_json(prompt)
        questions = _plain_list(_string_list(data.get("questions"), "questions"))
        if not questions:
            raise OrchestratorError("the engine returned no interview questions")

        session = Session(
            id=uuid.uuid4().hex,
            topic=topic,
            stage=SessionStage.INTERVIEW,
            interview_questions=questions,
            reading_grade=grade,
        )
        _logger.info("started session %s for topic %r (grade %d)", session.id, topic[:80], grade)
        return self._store.create(session)

    # --- Stage 2: mission + ladder ------------------------------------------

    def submit_interview(
        self, session_id: str, answers: list[str], grade: int | None = None
    ) -> Session:
        """Take the interview answers and generate the mission + project ladder.

        Args:
            session_id: The session being answered.
            answers: The learner's answers, aligned to ``interview_questions``.
            grade: Optional school grade level; when given, it updates the session
                so this and later stages write at that level.

        Returns:
            The session, stage ``LADDER``, with ``mission`` and ``projects`` set
            (the first rung ``ACTIVE``, the rest ``LOCKED``) and a day-one
            progress baseline recorded.

        Raises:
            OrchestratorError: If the session is not awaiting an interview, or the
                engine's reply cannot be parsed into a mission and ladder.
        """
        session = self._store.get(session_id)
        _require_stage(session, SessionStage.INTERVIEW)
        grade = _apply_grade(session, grade)

        qa_pairs = _zip_qa(session.interview_questions, answers)
        settings = get_settings()
        prompt = mission_and_ladder_prompt(
            session.topic,
            qa_pairs,
            settings.ladder_min_projects,
            settings.ladder_max_projects,
            [entry.note for entry in session.progress],
            grade,
        )
        data = self._ask_json(prompt)

        mission = _plain_text(str(data.get("mission", "")).strip())
        if not mission:
            raise OrchestratorError("the engine returned an empty mission")
        baseline = _plain_text(str(data.get("baseline", "")).strip())
        projects = _parse_projects(data.get("projects"))

        # First rung is ready to build; the rest stay locked until teach-backs pass.
        projects[0].status = ProjectStatus.ACTIVE
        session.mission = mission
        session.projects = projects
        session.current_project_id = projects[0].id
        session.stage = SessionStage.LADDER
        session.progress.append(
            ProgressEntry(on=_today(), note=_baseline_note(mission, baseline, qa_pairs))
        )
        _logger.info(
            "session %s: mission set, ladder of %d rungs", session.id, len(projects)
        )
        return self._store.save(session)

    # --- Stage 3: build (the browser watches the agent stream) --------------

    def build_instruction(
        self, session_id: str, project_id: str | None = None, grade: int | None = None
    ) -> str:
        """Return the build prompt for a rung and mark the session ``BUILDING``.

        The build itself is a live agent stream the server runs and the browser
        watches; this supplies the grounded, teaching build instruction and moves
        the rung to ``ACTIVE`` (if it wasn't already).

        Args:
            session_id: The session whose project to build.
            project_id: The rung to build; defaults to the current active rung.
            grade: Optional school grade level for the build narration; when given
                it updates the session.

        Returns:
            The natural-language build instruction for the agent.

        Raises:
            OrchestratorError: If the session/rung is unknown, or the rung is
                locked (an earlier rung's teach-back hasn't been passed yet).
        """
        session = self._store.get(session_id)
        project = self._resolve_project(session, project_id)
        if project.status is ProjectStatus.LOCKED:
            raise OrchestratorError(
                f"rung {project.id!r} is locked; finish the earlier project first"
            )
        grade = _apply_grade(session, grade)

        project.status = ProjectStatus.ACTIVE
        session.current_project_id = project.id
        session.stage = SessionStage.BUILDING
        self._store.save(session)
        _logger.info("session %s: building rung %s", session.id, project.id)
        return build_prompt(
            session.mission, project, self._prior_concepts(session, project), grade
        )

    def mark_built(self, session_id: str, project_id: str | None = None) -> Session:
        """Record that a rung's build finished; open its teach-back gate.

        Args:
            session_id: The session whose project was built.
            project_id: The rung that was built; defaults to the current rung.

        Returns:
            The session, stage ``TEACHBACK``, with the rung marked ``BUILT``.

        Raises:
            OrchestratorError: If the session/rung is unknown.
        """
        session = self._store.get(session_id)
        project = self._resolve_project(session, project_id)
        # A build after the gate already passed shouldn't un-complete the rung.
        if project.status is not ProjectStatus.COMPLETE:
            project.status = ProjectStatus.BUILT
            session.stage = SessionStage.TEACHBACK
            session.current_project_id = project.id
        _logger.info("session %s: rung %s built", session.id, project.id)
        return self._store.save(session)

    # --- Stage 4: teach-back gate -------------------------------------------

    def submit_teachback(
        self,
        session_id: str,
        project_id: str | None,
        explanation: str,
        grade: int | None = None,
    ) -> TeachBackResult:
        """Judge a teach-back; on a pass, unlock the next rung and log progress.

        Args:
            session_id: The session being assessed.
            project_id: The rung explained; defaults to the current rung.
            explanation: The learner's own-words explanation.
            grade: Optional school grade level for the verdict wording; when given
                it updates the session.

        Returns:
            A :class:`TeachBackResult` with the verdict, any probes, the progress
            note, and the unlocked next rung (if the gate opened).

        Raises:
            OrchestratorError: If the session/rung is unknown, the explanation is
                empty, or the engine's verdict cannot be parsed.
        """
        explanation = _clean(explanation, _MAX_EXPLANATION_CHARS, "explanation")
        session = self._store.get(session_id)
        project = self._resolve_project(session, project_id)
        grade = _apply_grade(session, grade)

        settings = get_settings()
        prompt = teachback_prompt(
            session.mission,
            project,
            explanation,
            self._prior_concepts(session, project),
            settings.teachback_max_probes,
            grade,
        )
        data = self._ask_json(prompt)

        passed = bool(data.get("passed", False))
        probes = _plain_list(_string_list(data.get("probes"), "probes"))
        feedback = _plain_text(str(data.get("feedback", "")).strip())
        progress_note = _plain_text(str(data.get("progress_note", "")).strip())
        storage_note = _plain_text(str(data.get("storage_note", "")).strip())

        teachback = TeachBack(
            project_id=project.id,
            explanation=explanation,
            probes=probes,
            feedback=feedback,
            passed=passed,
        )
        session.teachbacks.append(teachback)

        next_id: str | None = None
        if passed:
            project.status = ProjectStatus.COMPLETE
            if progress_note:
                session.progress.append(ProgressEntry(on=_today(), note=progress_note))
            next_id = self._unlock_next(session, project)
            session.stage = (
                SessionStage.COMPLETE if next_id is None else SessionStage.LADDER
            )
            session.current_project_id = next_id
        # A fail leaves the rung BUILT and the stage at TEACHBACK to try again.

        self._store.save(session)
        _logger.info(
            "session %s: teach-back on %s → %s",
            session.id,
            project.id,
            "passed" if passed else "not yet",
        )
        return TeachBackResult(
            teachback=teachback,
            passed=passed,
            progress_note=progress_note,
            storage_note=storage_note,
            next_project_id=next_id,
            stage=session.stage,
        )

    # --- Read ----------------------------------------------------------------

    def get_session(self, session_id: str) -> Session:
        """Return the stored session (for the browser to render or resume).

        Args:
            session_id: The session to fetch.

        Returns:
            The stored :class:`Session`.

        Raises:
            OrchestratorError: If no such session exists.
        """
        return self._store.get(session_id)

    def list_sessions(self) -> list[Session]:
        """Return every stored session, newest first (for a resume picker)."""
        return self._store.list_sessions()

    # --- Internals -----------------------------------------------------------

    def _ask_json(self, prompt: str) -> dict:
        """Ask the engine a JSON question and parse its reply.

        Runs the completion in a throwaway workspace — the orchestrator's own
        questions never need to write files. Any engine or parse failure surfaces
        as an :class:`OrchestratorError` the routes turn into a clean 4xx/5xx.
        """
        agent = self._agent or get_agent(
            get_settings().orchestrator_agent or get_settings().default_agent
        )
        workspace = create_ephemeral()
        answer = agent.complete(prompt, workspace)
        return extract_json(answer)

    def _resolve_project(self, session: Session, project_id: str | None) -> Project:
        """Return the named rung, or the current one when ``project_id`` is None."""
        target_id = project_id or session.current_project_id
        if target_id is None:
            raise OrchestratorError("no project specified and none is active")
        project = session.project(target_id)
        if project is None:
            raise OrchestratorError(
                f"session {session.id!r} has no rung {target_id!r}"
            )
        return project

    @staticmethod
    def _prior_concepts(session: Session, project: Project) -> list[str]:
        """Concepts from rungs before ``project`` — for interleaving/spacing."""
        concepts: list[str] = []
        for rung in session.projects:
            if rung.id == project.id:
                break
            concepts.append(rung.you_learn)
        return concepts

    @staticmethod
    def _unlock_next(session: Session, project: Project) -> str | None:
        """Set the rung after ``project`` to ``ACTIVE``; return its id or None."""
        ids = [p.id for p in session.projects]
        idx = ids.index(project.id)
        if idx + 1 < len(session.projects):
            nxt = session.projects[idx + 1]
            nxt.status = ProjectStatus.ACTIVE
            return nxt.id
        return None


# --- Module-level helpers (pure; no state) -----------------------------------


def _clean(text: str, cap: int, label: str) -> str:
    """Trim and validate a piece of learner free text."""
    cleaned = (text or "").strip()
    if not cleaned:
        raise OrchestratorError(f"{label} must not be empty")
    if len(cleaned) > cap:
        raise OrchestratorError(f"{label} is too long (max {cap} characters)")
    return cleaned


def _string_list(value: object, label: str) -> list[str]:
    """Coerce a JSON value into a clean list of non-empty strings."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise OrchestratorError(f"expected a list for {label}, got {type(value).__name__}")
    return [str(item).strip() for item in value if str(item).strip()]


def _parse_projects(value: object) -> list[Project]:
    """Turn the ladder JSON into validated :class:`Project` rungs.

    Missing ids are backfilled positionally ("p1", "p2", …) so a slightly loose
    reply still yields a usable, uniquely-keyed ladder.
    """
    if not isinstance(value, list) or not value:
        raise OrchestratorError("the engine returned no ladder projects")
    projects: list[Project] = []
    for index, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            raise OrchestratorError(f"ladder rung {index} is not an object")
        projects.append(
            Project(
                id=str(raw.get("id") or f"p{index}").strip(),
                you_build=_plain_text(str(raw.get("you_build", "")).strip()),
                you_learn=_plain_text(str(raw.get("you_learn", "")).strip()),
                done_when=_plain_text(str(raw.get("done_when", "")).strip()),
                status=ProjectStatus.LOCKED,
            )
        )
    _dedupe_ids(projects)
    return projects


def _dedupe_ids(projects: list[Project]) -> None:
    """Ensure rung ids are unique (positional suffix on any collision)."""
    seen: set[str] = set()
    for index, project in enumerate(projects, start=1):
        if project.id in seen:
            project.id = f"p{index}"
        seen.add(project.id)


def _zip_qa(questions: list[str], answers: list[str]) -> list[tuple[str, str]]:
    """Pair each interview question with its answer (missing answers → empty)."""
    return [
        (question, answers[i] if i < len(answers) else "")
        for i, question in enumerate(questions)
    ]


def _baseline_note(
    mission: str, baseline: str, qa_pairs: list[tuple[str, str]]
) -> str:
    """Compose the day-one progress baseline.

    Prefers the engine's explicit ``baseline`` phrase (it read the whole
    interview, so it's robust to a variable-length/reordered interview). Falls
    back to the first non-empty interview answer, then to a generic phrase — the
    old fixed "second answer" assumption no longer holds now the interview is
    adaptive.
    """
    starting = baseline or _first_answer(qa_pairs) or "starting fresh"
    return _plain_text(f"Day one, mission: {mission}. Starting point: {starting}")


def _first_answer(qa_pairs: list[tuple[str, str]]) -> str:
    """Return the first non-empty interview answer, or '' if there are none."""
    return next((a.strip() for _, a in qa_pairs if a and a.strip()), "")


def _require_stage(session: Session, expected: SessionStage) -> None:
    """Raise unless the session is at the ``expected`` stage."""
    if session.stage is not expected:
        raise OrchestratorError(
            f"session {session.id!r} is at stage {session.stage.value!r}, "
            f"expected {expected.value!r}"
        )


def _today() -> date:
    """Today's date (UTC) — progress entries are day-granular."""
    return datetime.now(timezone.utc).date()


# --- Plain-language + reading-grade helpers ----------------------------------

# Grades outside this range make no sense for wording; clamp so a stray value
# can't produce an absurd prompt.
_MIN_GRADE = 1
_MAX_GRADE = 20


def _clamp_grade(grade: int) -> int:
    """Keep a reading grade within a sensible school range."""
    try:
        return max(_MIN_GRADE, min(_MAX_GRADE, int(grade)))
    except (TypeError, ValueError):
        return get_settings().reading_grade_default


def _apply_grade(session: Session, grade: int | None) -> int:
    """Update the session's reading grade if a new one is given; return it.

    Lets the browser change the "explain like grade N" level at any time; when no
    grade is passed, the session keeps whatever it was created or last set with.
    """
    if grade is not None:
        session.reading_grade = _clamp_grade(grade)
    return session.reading_grade


def _plain_text(text: str) -> str:
    """Strip em/en dashes from generated text.

    A backstop for the prompt's "no dashes" rule so the user never sees an em dash
    in AI output regardless of how well the model followed instructions. Em dashes
    become commas; en dashes in ranges become hyphens.
    """
    return (
        text.replace(" — ", ", ")
        .replace(" – ", ", ")
        .replace("—", ", ")
        .replace("–", "-")
    )


def _plain_list(items: list[str]) -> list[str]:
    """Apply :func:`_plain_text` to every string in a list."""
    return [_plain_text(item) for item in items]
