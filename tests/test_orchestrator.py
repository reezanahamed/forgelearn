"""Offline tests for the learning-method orchestrator (Phase 7).

These drive the whole state machine — interview → ladder → build → teach-back →
unlock — with a scripted fake agent, so no subprocess or real CLI runs. The fake
yields its canned reply as narration, which exercises the real path end to end:
:meth:`AgentAdapter.complete` collecting narration, the JSON extraction, and the
engine's transitions.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from forgelearn.agents.base import AgentAdapter, RawEvent
from forgelearn.common.errors import AgentError, OrchestratorError
from forgelearn.common.types import AgentEvent, EventKind, ProjectStatus, SessionStage
from forgelearn.config import get_settings
from forgelearn.orchestrator import Orchestrator, extract_json
from forgelearn.orchestrator.store import SessionStore

# --- Fakes -------------------------------------------------------------------


class ScriptedAgent(AgentAdapter):
    """An agent that replies with queued text (as narration), in call order.

    Exercises the real :meth:`AgentAdapter.complete` + JSON parsing pipeline: the
    orchestrator calls ``complete``, which drains these events and returns the
    joined narration for the engine to parse.
    """

    name = "scripted"

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)
        self.prompts: list[str] = []

    def run(self, prompt: str, workspace: Path) -> Iterator[RawEvent]:  # pragma: no cover
        raise NotImplementedError("scripted overrides run_events")

    def run_events(self, prompt: str, workspace: Path) -> Iterator[AgentEvent]:
        self.prompts.append(prompt)
        assert self._replies, "ScriptedAgent ran out of scripted replies"
        text = self._replies.pop(0)
        yield AgentEvent(kind=EventKind.NARRATION, text=text)
        yield AgentEvent(kind=EventKind.DONE, text="ok")


_QUESTIONS = '{"questions": ["Why?", "What do you already know?", "Hours per week?"]}'
_LADDER = """Here is your plan:
```json
{"mission": "Learn RL to build a trading agent",
 "projects": [
   {"id": "p1", "you_build": "a bandit", "you_learn": "exploration vs exploitation", "done_when": "beats random"},
   {"id": "p2", "you_build": "q-learning", "you_learn": "the Bellman update", "done_when": "solves FrozenLake"}
 ]}
```"""
_TEACHBACK_FAIL = '{"passed": false, "probes": ["What does epsilon control?"], "feedback": "Close.", "progress_note": "", "storage_note": "Revisit tomorrow."}'
_TEACHBACK_PASS = '{"passed": true, "probes": [], "feedback": "Nailed it.", "progress_note": "You can now build a bandit from scratch.", "storage_note": "Space it out."}'
_TEACHBACK_PASS_LAST = '{"passed": true, "probes": [], "feedback": "Excellent.", "progress_note": "You now understand the Bellman update.", "storage_note": "Teach a friend."}'


@pytest.fixture()
def store() -> SessionStore:
    """A fresh, isolated in-memory store per test."""
    return SessionStore()


@pytest.fixture(autouse=True)
def _tmp_workspaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point ephemeral workspaces (used by orchestrator completions) at tmp."""
    monkeypatch.setattr(get_settings(), "workspace_dir", tmp_path)


def _orc(store: SessionStore, replies: list[str]) -> tuple[Orchestrator, ScriptedAgent]:
    agent = ScriptedAgent(replies)
    return Orchestrator(agent=agent, store=store), agent


# --- Interview ---------------------------------------------------------------


def test_start_generates_interview_questions(store: SessionStore) -> None:
    """Starting a session captures the topic and asks the interview questions."""
    orc, _ = _orc(store, [_QUESTIONS])
    session = orc.start("reinforcement learning")

    assert session.stage is SessionStage.INTERVIEW
    assert session.topic == "reinforcement learning"
    assert len(session.interview_questions) == 3
    assert store.get(session.id).id == session.id  # persisted


def test_start_rejects_empty_topic(store: SessionStore) -> None:
    """An empty topic is a clean orchestrator error, not a crash."""
    orc, _ = _orc(store, [_QUESTIONS])
    with pytest.raises(OrchestratorError):
        orc.start("   ")


# --- Mission + ladder --------------------------------------------------------


def test_submit_interview_builds_mission_and_ladder(store: SessionStore) -> None:
    """Answers yield a mission, a ladder (first rung active), and a baseline."""
    orc, _ = _orc(store, [_QUESTIONS, _LADDER])
    session = orc.start("rl")
    session = orc.submit_interview(session.id, ["a job", "the basics", "10"])

    assert session.stage is SessionStage.LADDER
    assert session.mission.startswith("Learn RL")
    assert [p.id for p in session.projects] == ["p1", "p2"]
    assert session.projects[0].status is ProjectStatus.ACTIVE
    assert session.projects[1].status is ProjectStatus.LOCKED
    assert session.current_project_id == "p1"
    # A day-one baseline is logged so later progress compares against it.
    assert len(session.progress) == 1
    assert "Day one" in session.progress[0].note


def test_start_stores_and_applies_reading_grade(store: SessionStore) -> None:
    """A chosen grade is saved on the session and sent to the engine."""
    orc, agent = _orc(store, [_QUESTIONS])
    session = orc.start("rl", grade=5)
    assert session.reading_grade == 5
    assert "GRADE 5" in agent.prompts[0]


def test_grade_override_on_interview_updates_session(store: SessionStore) -> None:
    """Passing a new grade later changes the session and the next prompt."""
    orc, agent = _orc(store, [_QUESTIONS, _LADDER])
    session = orc.start("rl", grade=7)
    session = orc.submit_interview(session.id, ["a", "b", "c"], grade=5)
    assert session.reading_grade == 5
    assert "GRADE 5" in agent.prompts[-1]


def test_generated_content_is_stripped_of_em_dashes(store: SessionStore) -> None:
    """Any em dash the model returns is removed before it reaches the learner."""
    dashed = (
        '{"mission": "Learn X — fast", "baseline": "brand new — zero prior",'
        ' "projects": [{"id": "p1", "you_build": "a — thing", "you_learn": "the — idea",'
        ' "done_when": "it — runs"}]}'
    )
    orc, _ = _orc(store, [_QUESTIONS, dashed])
    session = orc.start("rl")
    session = orc.submit_interview(session.id, ["a", "b", "c"])
    assert "—" not in session.mission
    assert "—" not in session.projects[0].you_build
    assert "—" not in session.projects[0].you_learn
    assert "—" not in session.projects[0].done_when
    assert all("—" not in entry.note for entry in session.progress)


def test_baseline_prefers_explicit_engine_phrase(store: SessionStore) -> None:
    """The day-one baseline uses the engine's explicit `baseline`, not a guess."""
    ladder = (
        '{"mission": "Learn RL", "baseline": "complete beginner, no ML background",'
        ' "projects": [{"id": "p1", "you_build": "a bandit", "you_learn": "explore",'
        ' "done_when": "beats random"}]}'
    )
    orc, _ = _orc(store, [_QUESTIONS, ladder])
    session = orc.start("rl")
    session = orc.submit_interview(session.id, ["for research", "none", "5"])
    assert "complete beginner, no ML background" in session.progress[0].note


def test_baseline_falls_back_to_first_answer(store: SessionStore) -> None:
    """With no explicit baseline, it falls back to a real answer, never crashes."""
    # _LADDER carries no "baseline" field → fallback path.
    orc, _ = _orc(store, [_QUESTIONS, _LADDER])
    session = orc.start("rl")
    session = orc.submit_interview(session.id, ["a job", "the basics", "10"])
    note = session.progress[0].note
    assert "Day one" in note
    assert "a job" in note  # first non-empty answer, not a fixed index


def test_submit_interview_wrong_stage_is_rejected(store: SessionStore) -> None:
    """Answering an interview twice (wrong stage) fails cleanly."""
    orc, _ = _orc(store, [_QUESTIONS, _LADDER])
    session = orc.start("rl")
    orc.submit_interview(session.id, ["x", "y", "z"])
    with pytest.raises(OrchestratorError):
        orc.submit_interview(session.id, ["again"])


# --- Build -------------------------------------------------------------------


def test_build_instruction_is_grounded_and_marks_building(store: SessionStore) -> None:
    """The build prompt names the rung + mission and moves to the build stage."""
    orc, _ = _orc(store, [_QUESTIONS, _LADDER])
    session = orc.start("rl")
    session = orc.submit_interview(session.id, ["a", "b", "c"])

    prompt = orc.build_instruction(session.id, "p1")
    assert "a bandit" in prompt  # you_build
    assert "Learn RL" in prompt  # grounded in the mission
    assert store.get(session.id).stage is SessionStage.BUILDING


def test_build_locked_rung_is_rejected(store: SessionStore) -> None:
    """A locked later rung cannot be built before earlier ones are passed."""
    orc, _ = _orc(store, [_QUESTIONS, _LADDER])
    session = orc.start("rl")
    session = orc.submit_interview(session.id, ["a", "b", "c"])
    with pytest.raises(OrchestratorError):
        orc.build_instruction(session.id, "p2")


def test_mark_built_opens_teachback(store: SessionStore) -> None:
    """A finished build marks the rung built and opens the teach-back gate."""
    orc, _ = _orc(store, [_QUESTIONS, _LADDER])
    session = orc.start("rl")
    session = orc.submit_interview(session.id, ["a", "b", "c"])
    orc.build_instruction(session.id, "p1")
    session = orc.mark_built(session.id, "p1")

    assert session.project("p1").status is ProjectStatus.BUILT
    assert session.stage is SessionStage.TEACHBACK


# --- Teach-back gate ---------------------------------------------------------


def test_teachback_fail_keeps_the_gate_shut(store: SessionStore) -> None:
    """A weak explanation does not unlock the next rung."""
    orc, _ = _orc(store, [_QUESTIONS, _LADDER, _TEACHBACK_FAIL])
    session = orc.start("rl")
    session = orc.submit_interview(session.id, ["a", "b", "c"])
    orc.build_instruction(session.id, "p1")
    orc.mark_built(session.id, "p1")

    result = orc.submit_teachback(session.id, "p1", "it explores randomly I think")
    assert result.passed is False
    assert result.teachback.probes  # probes were raised
    session = store.get(session.id)
    assert session.project("p1").status is ProjectStatus.BUILT  # still not complete
    assert session.project("p2").status is ProjectStatus.LOCKED
    assert session.stage is SessionStage.TEACHBACK


def test_teachback_pass_unlocks_next_and_logs_progress(store: SessionStore) -> None:
    """A strong explanation completes the rung, unlocks the next, logs progress."""
    orc, _ = _orc(store, [_QUESTIONS, _LADDER, _TEACHBACK_PASS])
    session = orc.start("rl")
    session = orc.submit_interview(session.id, ["a", "b", "c"])
    orc.build_instruction(session.id, "p1")
    orc.mark_built(session.id, "p1")

    result = orc.submit_teachback(session.id, "p1", "epsilon trades off explore/exploit…")
    assert result.passed is True
    assert result.next_project_id == "p2"
    assert result.storage_note  # a durability tip came back

    session = store.get(session.id)
    assert session.project("p1").status is ProjectStatus.COMPLETE
    assert session.project("p2").status is ProjectStatus.ACTIVE
    assert session.current_project_id == "p2"
    assert session.stage is SessionStage.LADDER
    # Baseline + the passing progress note, compared only to day one.
    assert len(session.progress) == 2
    assert "build a bandit" in session.progress[1].note


def test_passing_the_last_rung_completes_the_ladder(store: SessionStore) -> None:
    """Passing the final rung marks the whole session complete."""
    orc, _ = _orc(
        store, [_QUESTIONS, _LADDER, _TEACHBACK_PASS, _TEACHBACK_PASS_LAST]
    )
    session = orc.start("rl")
    session = orc.submit_interview(session.id, ["a", "b", "c"])
    orc.build_instruction(session.id, "p1")
    orc.mark_built(session.id, "p1")
    orc.submit_teachback(session.id, "p1", "…")

    orc.build_instruction(session.id, "p2")
    orc.mark_built(session.id, "p2")
    result = orc.submit_teachback(session.id, "p2", "…")

    assert result.passed is True
    assert result.next_project_id is None
    assert result.stage is SessionStage.COMPLETE
    session = store.get(session.id)
    assert all(p.status is ProjectStatus.COMPLETE for p in session.projects)


def test_teachback_prompt_carries_prior_concepts(store: SessionStore) -> None:
    """The teach-back on a later rung spaces/interleaves an earlier concept."""
    orc, agent = _orc(
        store, [_QUESTIONS, _LADDER, _TEACHBACK_PASS, _TEACHBACK_PASS_LAST]
    )
    session = orc.start("rl")
    session = orc.submit_interview(session.id, ["a", "b", "c"])
    orc.build_instruction(session.id, "p1")
    orc.mark_built(session.id, "p1")
    orc.submit_teachback(session.id, "p1", "…")
    orc.build_instruction(session.id, "p2")
    orc.mark_built(session.id, "p2")
    orc.submit_teachback(session.id, "p2", "…")

    # The last prompt the agent saw was p2's teach-back; it should reference the
    # earlier concept (exploration) for spacing/interleaving.
    assert "exploration vs exploitation" in agent.prompts[-1]


# --- Robustness --------------------------------------------------------------


def test_bad_json_reply_is_a_clean_error(store: SessionStore) -> None:
    """A non-JSON engine reply surfaces as an OrchestratorError, not a crash."""
    orc, _ = _orc(store, ["I could not do that, sorry."])
    with pytest.raises(OrchestratorError):
        orc.start("rl")


def test_empty_agent_answer_is_reported(store: SessionStore) -> None:
    """An engine that returns no text at all fails loudly via complete()."""

    class _Silent(AgentAdapter):
        name = "silent"

        def run(self, prompt: str, workspace: Path):  # pragma: no cover
            raise NotImplementedError

        def run_events(self, prompt: str, workspace: Path) -> Iterator[AgentEvent]:
            yield AgentEvent(kind=EventKind.DONE, text="")

    with pytest.raises(AgentError):
        Orchestrator(agent=_Silent(), store=store).start("rl")


# --- JSON extraction ---------------------------------------------------------


def test_extract_json_from_fenced_block() -> None:
    """A fenced ```json block is unwrapped and parsed."""
    assert extract_json('prose\n```json\n{"a": 1}\n```\nmore') == {"a": 1}


def test_extract_json_from_surrounding_prose() -> None:
    """A bare object with a leading sentence is still recovered."""
    assert extract_json('Sure! {"a": 1, "b": 2} hope that helps') == {"a": 1, "b": 2}


def test_extract_json_raises_when_absent() -> None:
    """No object anywhere is a clean error."""
    with pytest.raises(OrchestratorError):
        extract_json("there is no json here")
