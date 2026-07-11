"""Tests for the teaching prompts — the method's contract, in isolation.

The prompts are pure functions, so these assert the *instructions* they carry
without running any engine. The focus here is the adaptive interview: it must be
a bounded range (not a fixed count) and must direct the tutor to uncover the
learner's purpose/depth and current level, which is what sizes the ladder.
"""

from __future__ import annotations

from forgelearn.common.types import Project
from forgelearn.orchestrator.prompts import (
    build_prompt,
    interview_prompt,
    mission_and_ladder_prompt,
    teachback_prompt,
)


def test_interview_prompt_is_adaptive_within_bounds() -> None:
    """It asks for a range of questions, only as many as needed — not a fixed N."""
    prompt = interview_prompt("reinforcement learning", 3, 6)
    assert "between 3 and 6" in prompt
    # It must not force a fixed number.
    assert "exactly" not in prompt.lower()
    assert "as you truly need" in prompt.lower()


def test_interview_prompt_captures_purpose_and_level() -> None:
    """It directs the tutor to learn WHY (depth) and the current KNOWLEDGE LEVEL."""
    prompt = interview_prompt("the French Revolution", 3, 6)
    lowered = prompt.lower()
    # Purpose / target depth (casual vs academic vs career vs research).
    assert "purpose" in lowered
    assert "research" in lowered and "academic" in lowered
    # Current level for ZPD sizing.
    assert "current level" in lowered or "already know" in lowered
    assert "time" in lowered


def test_ladder_prompt_scales_rigor_to_purpose() -> None:
    """The ladder is told to let the learner's purpose/depth set how far it reaches."""
    prompt = mission_and_ladder_prompt(
        "quantum computing",
        [("Why?", "for my research"), ("Level?", "grad student")],
        5,
        7,
        [],
    )
    lowered = prompt.lower()
    assert "purpose" in lowered
    assert "research" in lowered


def test_every_prompt_carries_the_reading_grade_and_no_em_dash() -> None:
    """Each prompt tells the model the grade level and is itself em-dash-free."""
    project = Project(id="p1", you_build="a bandit", you_learn="explore", done_when="runs")
    prompts = [
        interview_prompt("rl", 3, 6, 5),
        mission_and_ladder_prompt("rl", [("q", "a")], 5, 7, [], 5),
        build_prompt("mission", project, [], 5),
        teachback_prompt("mission", project, "my explanation", [], 3, 5),
    ]
    for prompt in prompts:
        assert "GRADE 5" in prompt  # the model is told the level
        assert "em dash" in prompt.lower()  # ... and told not to use them
        assert "—" not in prompt  # the prompt text itself uses no em dash
