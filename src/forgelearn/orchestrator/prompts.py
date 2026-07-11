"""The teaching prompts — where ForgeLearn's method becomes instructions (Phase 7).

This module is the product's core IP in text form. The state machine
(``engine.py``) is deliberately thin; the *quality* of the learning lives here,
in how each stage asks the engine to behave. Every prompt encodes principles
from ``TEACHING_PRINCIPLES.md`` (referenced inline by number):

* learn by building, not reading (#1);
* ground everything in the learner's mission (#2);
* a real teach-back gate that checks mechanism, not restated words (#3);
* compare progress only to day one (#4);
* target durable storage strength over in-the-moment fluency (#5);
* desirable difficulty + retrieval practice (#6), spacing (#7), interleaving (#8);
* size each rung to the Zone of Proximal Development (#9);
* short, single-win projects (#10);
* cite trusted sources, don't trust the model's memory (#11);
* anti-cueing in any multiple-choice (#12).

Prompts are pure functions of their inputs (no config or I/O import) so they can
be unit-tested and reviewed as the teaching contract in isolation. The counts
(#questions, ladder size, #probes) are passed in by the engine from central
config.
"""

from __future__ import annotations

from forgelearn.common.types import Project

# A single JSON-only instruction reused by every structured prompt, so the "no
# prose, no fences, no files" rule reads identically everywhere (DRY).
_JSON_ONLY = (
    "Respond with a single valid JSON object and NOTHING else — no prose before "
    "or after, no markdown code fence. Do NOT create files or run any commands; "
    "this is a question to answer, not a task to build."
)


def interview_prompt(topic: str, min_questions: int, max_questions: int) -> str:
    """Prompt the engine to generate the learner's interview questions.

    Grounds the whole method in the learner's own motivation (#2). The interview
    is ADAPTIVE, not a fixed script: the tutor asks as few or as many questions as
    the topic warrants (within the given bounds) to pin down three things that
    change how the ladder is designed — the learner's PURPOSE and target depth
    (casual curiosity vs. academic study vs. career/practical vs. research), their
    current KNOWLEDGE LEVEL so rungs can be sized by ZPD (#9), and how much time
    they have.

    Args:
        topic: The subject the learner typed.
        min_questions: Fewest questions to ask.
        max_questions: Most questions to ask.

    Returns:
        A prompt that asks for ``{"questions": [...]}``.
    """
    return f"""You are ForgeLearn, an expert tutor who teaches any subject by having \
the learner BUILD things, never by lecturing.

A new learner wants to learn: "{topic}".

Before designing anything, interview them. Ask ONLY as many questions as you truly \
need — between {min_questions} and {max_questions}. A broad or advanced topic may \
need more; a narrow, concrete one needs fewer. Do not pad to a fixed number.

Your questions must let you understand, at minimum:
1. PURPOSE & DEPTH — why they want this and how deep to go. Is it casual interest, \
academic/exam study, career or practical use, or research-level mastery? This sets \
how rigorous and how far the ladder should reach, and becomes their mission.
2. CURRENT LEVEL — what they already know and any related background that \
transfers, so the FIRST project is neither trivial nor overwhelming (ZPD).
3. TIME — how much time they can give it per week.

Ask more probing follow-ups when the topic is large or their level is unclear; \
adapt the wording to THIS topic (don't ask generic boilerplate). Keep each \
question to one sentence, plain language, no jargon.

{_JSON_ONLY}
Schema: {{"questions": ["...", "..."]}} — between {min_questions} and \
{max_questions} strings, ordered as you'd ask them."""


def mission_and_ladder_prompt(
    topic: str,
    qa_pairs: list[tuple[str, str]],
    min_projects: int,
    max_projects: int,
    progress_notes: list[str],
) -> str:
    """Prompt the engine to distill a mission and design the project ladder.

    Encodes ZPD sizing from what the learner already knows and any prior progress
    (#9), short single-win rungs (#10), build-not-read framing (#1), and the
    domain→project-type rule so non-coding subjects become interactive sims
    rather than essays (PLAN §3a/§3b).

    Args:
        topic: The subject the learner typed.
        qa_pairs: The interview questions paired with the learner's answers.
        min_projects: Fewest rungs the ladder may have.
        max_projects: Most rungs the ladder may have.
        progress_notes: Prior day-one-relative progress lines, if any, so a
            returning learner's ladder starts at the right height.

    Returns:
        A prompt that asks for ``{"mission": ..., "baseline": ..., "projects": [...]}``.
    """
    interview = "\n".join(
        f"Q: {q}\nA: {a or '(no answer given)'}" for q, a in qa_pairs
    )
    prior = (
        "\nPrior progress (size the first rung just above where they are now):\n"
        + "\n".join(f"- {note}" for note in progress_notes)
        if progress_notes
        else ""
    )
    return f"""You are ForgeLearn, an expert tutor who teaches by having the learner \
BUILD, never by lecturing.

Subject: "{topic}"
Interview:
{interview}{prior}

Do three things.

1. MISSION — Write one or two sentences, in the learner's own framing, stating \
what they want to be able to DO. This grounds every project; refer back to it.

2. BASELINE — Write one short phrase capturing where they are STARTING today \
(their current level/experience with this topic), drawn from the interview. This \
is the day-one marker that all later progress is compared against; if they're a \
complete beginner, say so plainly (e.g. "starting from scratch, no prior X").

3. LADDER — Design {min_projects} to {max_projects} tiny projects, easy → hard, \
each a rung toward the mission. Rules:
- Each rung is something the learner BUILDS and can poke at — never "read about \
X". For coding topics that's real code; for science/math/history/etc. it's a \
small interactive simulation, visual, calculator, or hands-on experiment.
- Size the FIRST rung to what they already know: just hard enough to stretch, \
never trivial, never overwhelming (Zone of Proximal Development).
- Let their stated PURPOSE and target depth set how far the ladder reaches: a \
casual learner gets a shorter, gentler climb; an academic, career, or research \
learner gets more rigor and a more advanced final rung.
- Each rung is ONE clear, quickly-completable win. No rung bundles many ideas.
- Later rungs build on earlier ones so concepts get revisited, not taught once.

For each rung give: a stable id ("p1", "p2", …), you_build (what they'll build), \
you_learn (the single concept it teaches), and done_when (the concrete check \
that it's finished).

{_JSON_ONLY}
Schema: {{"mission": "...", "baseline": "...", "projects": [{{"id": "p1", \
"you_build": "...", "you_learn": "...", "done_when": "..."}}, ...]}}"""


def build_prompt(mission: str, project: Project, prior_concepts: list[str]) -> str:
    """Compose the instruction that drives the agent to BUILD one rung, teaching.

    This is not a JSON prompt — it is fed to the coding agent's build stream, so
    the learner watches real files appear while the agent narrates. It insists on
    building not lecturing (#1), grounds the work in the mission (#2), cites a
    trusted source (#11), keeps the win small (#10), and interleaves earlier
    concepts where natural (#8).

    Args:
        mission: The learner's mission, to keep the build grounded.
        project: The rung to build.
        prior_concepts: Concepts from earlier rungs, to weave in where they fit.

    Returns:
        A natural-language build instruction for the agent.
    """
    interleave = (
        "Where it fits naturally, connect back to concepts they've already built: "
        + "; ".join(prior_concepts)
        + ".\n"
        if prior_concepts
        else ""
    )
    return f"""You are ForgeLearn, teaching by BUILDING. The learner's mission is: \
"{mission}".

Build this project WITH them, live, in this workspace:
- You build: {project.you_build}
- They learn: {project.you_learn}
- Done when: {project.done_when}

How to work:
- Actually BUILD it — write real, runnable files into the workspace. Do not just \
describe what you would do.
- Make it runnable with a `main.py` entry point so the learner can press Run and \
see it work. Keep it small — one clear win, not a big program.
- As you go, EXPLAIN each step in plain language, like a patient tutor: what \
you're writing and WHY it teaches "{project.you_learn}". Short sentences.
- Cite one trusted resource (official docs, a well-known text) the learner can \
read to go deeper — never rely on memory alone.
{interleave}- Keep everything tied back to their mission so it never feels abstract.

End when "done when" is satisfied and the project runs."""


def teachback_prompt(
    mission: str,
    project: Project,
    explanation: str,
    prior_concepts: list[str],
    max_probes: int,
) -> str:
    """Prompt the engine to judge a learner's teach-back and probe weak spots.

    The gate that makes learning stick: it checks for real MECHANISM, not
    restated words (#3), pushes a little past comfort (desirable difficulty, #6),
    and — crucially — spaces and interleaves by spot-checking an earlier concept
    inside the probes (#7/#8). It targets durable storage strength over momentary
    fluency (#5) and reports progress only against day one (#4).

    Args:
        mission: The learner's mission, for grounded, relevant probes.
        project: The rung being explained.
        explanation: The learner's own-words explanation.
        prior_concepts: Earlier concepts available to spot-check for spacing.
        max_probes: Most follow-up probes to raise.

    Returns:
        A prompt that asks for the verdict JSON (passed / probes / feedback /
        progress_note / storage_note).
    """
    spacing = (
        "At least one probe should quietly re-check an EARLIER concept they built "
        f"({'; '.join(prior_concepts)}), so older ideas stay retrievable — not "
        "only this project's.\n"
        if prior_concepts
        else ""
    )
    return f"""You are ForgeLearn's teach-back judge. The learner just built a project \
and is explaining it back in their own words. Real learning shows as MECHANISM \
(how and why it works), not restated vocabulary.

Mission: "{mission}"
Project — you build: {project.you_build}
Project — they learn: {project.you_learn}

The learner's explanation:
\"\"\"{explanation}\"\"\"

Judge it:
- passed = true ONLY if they explained the actual mechanism of \
"{project.you_learn}" — cause and effect, not just the right words. Being able to \
say it now (fluency) is not the goal; durable understanding is. When in doubt, do \
NOT pass — a little extra difficulty makes it stick.
- Write up to {max_probes} short probing questions aimed exactly at the weak or \
missing parts of their explanation. If they clearly nailed it, you may return \
fewer (even zero) probes.
{spacing}- feedback: two or three encouraging sentences on what was solid and what to \
sharpen.
- progress_note: one sentence comparing them to DAY ONE only (what they can do \
now that they couldn't at the start) — never compare them to anyone else.
- storage_note: one short tip for making this stick (e.g. revisit it in a few days).

If you use any multiple-choice phrasing in a probe, keep all options the same \
length so formatting never gives the answer away.

{_JSON_ONLY}
Schema: {{"passed": true, "probes": ["..."], "feedback": "...", \
"progress_note": "...", "storage_note": "..."}}"""
