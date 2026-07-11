"""Teaching prompts for the guided-lesson redesign (Phase A).

The redesign changes the method from "the AI builds, you watch, you explain" to a
Brilliant-style loop where the learner is taught first and then builds:

    syllabus  ->  per lesson: explain + interactive widget -> check ->
                  AI demos a worked example -> learner rebuilds it -> review

Each function here is a pure prompt builder (no I/O), so the teaching contract can
be reviewed and unit-tested in isolation, exactly like the original prompts. They
reuse the shared plain-language rule (grade level, no em dashes) so generated text
stays simple and clean.
"""

from __future__ import annotations

from forgelearn.common.types import Lesson
from forgelearn.orchestrator.prompts import (
    DEFAULT_GRADE,
    _JSON_ONLY,
    _plain_language,
)


def syllabus_prompt(
    topic: str,
    qa_pairs: list[tuple[str, str]],
    min_lessons: int,
    max_lessons: int,
    grade: int = DEFAULT_GRADE,
) -> str:
    """Prompt the engine to design a mission and a syllabus of lessons.

    Unlike the old ladder (a list of things the AI builds), a syllabus is a list of
    LESSONS. Each lesson teaches one concept and then has the learner build a small
    thing themselves. The AI first demos a worked example (``demo_task``); the
    learner then builds their own version (``build_task``).

    Args:
        topic: The subject the learner typed.
        qa_pairs: The interview questions paired with the learner's answers.
        min_lessons: Fewest lessons in the syllabus.
        max_lessons: Most lessons in the syllabus.
        grade: School grade level to write for.

    Returns:
        A prompt that asks for ``{"mission": ..., "lessons": [...]}``.
    """
    interview = "\n".join(f"Q: {q}\nA: {a or '(no answer given)'}" for q, a in qa_pairs)
    return f"""You are ForgeLearn, a patient tutor. You teach a concept simply first, \
then have the learner BUILD it themselves (you never just lecture, and you never do \
all the building for them).

Subject: "{topic}"
Interview:
{interview}

Do two things.

1. MISSION: one or two sentences, in the learner's own framing, on what they want to \
be able to DO.

2. SYLLABUS: design {min_lessons} to {max_lessons} short lessons, easy to hard, that \
build toward the mission. Rules:
- Each lesson teaches ONE small concept, then the learner builds something tiny that \
uses it. One clear idea per lesson.
- Size the FIRST lesson to what they already know (not trivial, not overwhelming).
- Later lessons build on earlier ones.
- Pick domain_type per subject: "code" for programming topics (the learner writes \
real code), or "interactive" for science, math, history, and the like (the learner \
builds a small interactive simulation or visual).
- demo_task: the small worked example YOU will build and explain first.
- build_task: the similar-but-different thing the LEARNER then builds on their own \
(so they practice, not copy).

For each lesson give: a stable id ("u1", "u2", and so on), title, goal (one line on \
what they can do after), domain_type, demo_task, and build_task.

{_plain_language(grade)}

{_JSON_ONLY}
Schema: {{"mission": "...", "lessons": [{{"id": "u1", "title": "...", "goal": "...", \
"domain_type": "code", "demo_task": "...", "build_task": "..."}}, ...]}}"""


def lesson_content_prompt(mission: str, lesson: Lesson, grade: int = DEFAULT_GRADE) -> str:
    """Prompt the engine to generate one lesson's teaching content.

    Produces the three things the learner sees before building: a plain-English
    explanation with a concrete example, one interactive widget to play with, and a
    quick check question. The widget is a self-contained HTML document rendered in a
    sandboxed iframe, so it must run fully offline (inline CSS and JS only).

    Args:
        mission: The learner's mission, to keep the lesson grounded.
        lesson: The lesson to fill in (its title/goal frame the content).
        grade: School grade level to write for.

    Returns:
        A prompt that asks for ``{"concept": ..., "widget": {...}, "check": {...}}``.
    """
    return f"""You are ForgeLearn, teaching ONE small concept clearly, the way \
Brilliant does: show, don't just tell.

Mission: "{mission}"
Lesson: {lesson.title}
Goal: {lesson.goal}

Produce three things.

1. concept: a short, friendly explanation of this one idea, built around a CONCRETE \
example the learner can picture. A few short paragraphs at most. Show the idea; do \
not dump a wall of theory.

2. widget: ONE small interactive manipulative that lets the learner FEEL the concept \
(for example a slider that changes a value and updates a picture or number, a couple \
of buttons, or a tiny canvas). Return it as a single, fully self-contained HTML \
document: inline CSS and JavaScript only, NO external files, NO network requests, no \
CDN links. It must work offline inside a sandboxed iframe. Keep it small and focused \
on this one idea. Give it a short title and a one-line caption telling the learner \
what to try.

3. check: ONE quick question that checks they understood the idea (not memorized \
words). Use kind "mcq" with 2 to 4 short options of similar length, or kind "short" \
for a one-sentence answer.

{_plain_language(grade)}

{_JSON_ONLY}
Schema: {{"concept": "...", "widget": {{"title": "...", "caption": "...", "html": \
"<!doctype html>..."}}, "check": {{"question": "...", "kind": "mcq", "options": \
["...", "..."]}}}}"""


def check_judge_prompt(
    lesson: Lesson, question: str, answer: str, grade: int = DEFAULT_GRADE
) -> str:
    """Prompt the engine to judge the learner's answer to a lesson check.

    Kind, low-stakes grading: it is a comprehension check, not a gate. Encourage,
    correct gently, and always explain the right idea so a wrong answer still teaches.

    Args:
        lesson: The lesson being checked.
        question: The check question that was asked.
        answer: The learner's answer.
        grade: School grade level to write for.

    Returns:
        A prompt that asks for ``{"correct": bool, "feedback": ..., "explanation": ...}``.
    """
    return f"""You are ForgeLearn, checking a quick understanding question kindly. \
This is a low-stakes check, not a test.

Concept: {lesson.title} ({lesson.goal})
Question asked: "{question}"
The learner answered: \"\"\"{answer}\"\"\"

Judge it:
- correct: true if they showed the right idea (be generous with wording).
- feedback: one or two encouraging sentences on what they got right or where they \
slipped.
- explanation: briefly explain the correct idea in plain words, so even a wrong \
answer teaches them something.

{_plain_language(grade)}

{_JSON_ONLY}
Schema: {{"correct": true, "feedback": "...", "explanation": "..."}}"""


def demo_prompt(mission: str, lesson: Lesson, grade: int = DEFAULT_GRADE) -> str:
    """Compose the instruction that drives the agent to BUILD the worked example.

    Fed to the coding agent's build stream so the learner watches a full, working
    example appear while the agent narrates. For "interactive" lessons the example
    is a small browser simulation; for "code" lessons it is real runnable code.

    Args:
        mission: The learner's mission, to keep the demo grounded.
        lesson: The lesson whose demo to build.
        grade: School grade level to narrate for.

    Returns:
        A natural-language build instruction for the agent.
    """
    kind = (
        "a small, self-contained interactive simulation or visual (an index.html \
with inline JS/CSS) the learner can open and poke at"
        if lesson.domain_type == "interactive"
        else "small, real, runnable code with a `main.py` entry point"
    )
    return f"""You are ForgeLearn, teaching by showing a WORKED EXAMPLE. The learner's \
mission is: "{mission}".

Build this worked example live, in this workspace, and narrate as you go:
- Concept: {lesson.title} ({lesson.goal})
- Build: {lesson.demo_task}
- It should be {kind}.

How to work:
- Actually BUILD it: write real files into the workspace. Keep it small, one clear \
idea, so the learner can follow every line.
- Explain each step as you write it: what you are doing and WHY it shows the concept.
- Make it runnable (press Run should work) so the learner sees the result.
- This is the EXAMPLE. The learner will build their own version next, so keep it \
clear and typical, not clever.
- {_plain_language(grade)}

End when it runs and the example is complete."""


def build_review_prompt(
    mission: str, lesson: Lesson, files_summary: str, grade: int = DEFAULT_GRADE
) -> str:
    """Prompt the engine to review the learner's own build and coach them.

    The learner has just built their version of ``build_task``. The AI reads their
    files and coaches: does it meet the goal, what is good, what to fix, with hints
    rather than the full solution.

    Args:
        mission: The learner's mission, for grounded review.
        lesson: The lesson being built.
        files_summary: The learner's workspace files and their contents.
        grade: School grade level to write for.

    Returns:
        A prompt asking for ``{"passed": bool, "feedback": ..., "hints": [...],
        "progress_note": ...}``.
    """
    return f"""You are ForgeLearn, reviewing what the LEARNER built themselves and \
coaching them. Do NOT rewrite it for them; guide them to fix it.

Mission: "{mission}"
Lesson: {lesson.title} ({lesson.goal})
What they were asked to build: {lesson.build_task}

Their files:
{files_summary}

Judge and coach:
- passed: true only if their build genuinely meets the task and works. When close but \
not there, pass = false so they finish it themselves.
- feedback: two or three encouraging sentences on what is working and what is off.
- hints: up to three specific, actionable hints toward the fix. Point at the line or \
idea; do NOT paste the full solution.
- progress_note: one sentence on what they can now do that they could not at the \
start (compare to day one only, never to other people).

{_plain_language(grade)}

{_JSON_ONLY}
Schema: {{"passed": true, "feedback": "...", "hints": ["..."], "progress_note": "..."}}"""


def hint_prompt(
    mission: str, lesson: Lesson, files_summary: str, grade: int = DEFAULT_GRADE
) -> str:
    """Prompt the engine for a single next hint on the learner's current build.

    Args:
        mission: The learner's mission.
        lesson: The lesson being built.
        files_summary: The learner's current files (may be empty if not started).
        grade: School grade level to write for.

    Returns:
        A prompt asking for ``{"hint": ...}``.
    """
    return f"""You are ForgeLearn, giving the learner ONE small nudge, not the answer.

Mission: "{mission}"
Lesson: {lesson.title} ({lesson.goal})
Task they are building: {lesson.build_task}

Their work so far:
{files_summary}

Give ONE short, specific hint for their very next step. Do not write the solution; \
help them take the next small step themselves.

{_plain_language(grade)}

{_JSON_ONLY}
Schema: {{"hint": "..."}}"""
