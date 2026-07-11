"""Pull a JSON object out of a language model's free-text answer (Phase 7).

The orchestrator asks the engine for structured data (interview questions, a
ladder, a teach-back verdict) by instructing it to "reply with JSON only". Models
mostly obey, but sometimes wrap the object in a `````json`` fence or add a stray
sentence before it. This module isolates that fragility in one place: it strips
a fenced block if present, else falls back to the first ``{`` … matching ``}``
span, then parses. A response with no valid object raises
:class:`~forgelearn.common.errors.OrchestratorError` so the caller fails loudly
instead of proceeding on half-parsed data.
"""

from __future__ import annotations

import json
import re

from forgelearn.common.errors import OrchestratorError
from forgelearn.common.logging import get_logger

_logger = get_logger("orchestrator.parsing")

# A fenced code block, optionally tagged (```json … ```). Group 1 is its body.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)

# Max characters of a bad response echoed into the error/log — enough to debug,
# not so much it floods the line.
_ECHO_CHARS = 300


def extract_json(text: str) -> dict:
    """Return the first JSON object found in ``text``.

    Resolution order: a fenced ```json block, then the widest ``{`` … ``}`` span,
    then the whole string. The first candidate that parses into a JSON *object*
    wins.

    Args:
        text: The model's raw answer.

    Returns:
        The parsed object as a dict.

    Raises:
        OrchestratorError: If no candidate parses into a JSON object.
    """
    for candidate in _candidates(text):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
        _logger.debug("parsed JSON was not an object (%s); trying next", type(parsed))
    raise OrchestratorError(
        f"expected a JSON object in the model's reply but found none: "
        f"{text[:_ECHO_CHARS]!r}"
    )


def _candidates(text: str) -> list[str]:
    """Yield JSON-object candidate substrings from ``text``, best guess first."""
    candidates: list[str] = []
    fence = _FENCE_RE.search(text)
    if fence:
        candidates.append(fence.group(1))
    # Widest brace span: from the first '{' to the last '}'. Handles a leading
    # sentence or a trailing note around the object.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])
    candidates.append(text.strip())
    return candidates
