"""Export a finished learning session as a self-contained HTML file (Phase 8).

A learner should be able to keep what they built: this renders one standalone
``.html`` document — no external CSS, JS, fonts, or image requests — that
captures the whole session (mission, ladder, progress vs day one, teach-backs)
and inlines every file the agent built into that session's workspace. Text files
are embedded in ``<pre>`` blocks; binary assets (images) are inlined as ``data:``
URIs so the page works fully offline (PLAN §8a). If the project has an
``index.html``, its markup is previewed in a **sandboxed** ``<iframe srcdoc>`` —
sandboxed with no ``allow-scripts``, so HTML/CSS layout renders but embedded
JavaScript does NOT run (auto-executing exported code on open would be unsafe);
the full source is always shown below the preview.

All styling is inlined in a single ``<style>`` block; every piece of
learner/agent text is HTML-escaped, so an export can never inject markup.
"""

from __future__ import annotations

import base64
from html import escape

from forgelearn.common.logging import get_logger
from forgelearn.common.types import ProjectStatus, Session
from forgelearn.workspace import list_files, read_bytes

_logger = get_logger("storage.export")

# Suffix → MIME type for assets inlined as data: URIs. Anything not listed is
# shown as text if it decodes as UTF-8, else summarized as an opaque binary.
_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}

# A sandboxed HTML preview is offered for a project whose entry file is one of
# these (a browser simulation/toy per PLAN §3a). The iframe is sandboxed without
# allow-scripts, so its markup renders but its JavaScript never executes.
_LIVE_PREVIEW_NAMES = ("index.html",)

# Human labels for the ladder status badges in the exported page.
_STATUS_LABEL = {
    ProjectStatus.LOCKED: "locked",
    ProjectStatus.ACTIVE: "up next",
    ProjectStatus.BUILT: "built",
    ProjectStatus.COMPLETE: "done",
}

_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  margin: 0; padding: 2rem 1.25rem; max-width: 900px; margin-inline: auto;
  color: #1a1a1a; background: #fbfbfa;
}
header { border-bottom: 2px solid #6c5ce7; padding-bottom: 1rem; margin-bottom: 1.5rem; }
h1 { margin: 0 0 .25rem; font-size: 1.6rem; }
h2 { font-size: 1.2rem; margin: 2rem 0 .75rem; border-bottom: 1px solid #e5e5e5; padding-bottom: .3rem; }
.tag { color: #6c5ce7; font-weight: 600; }
.muted { color: #666; font-size: .9rem; }
.mission { font-size: 1.15rem; font-weight: 600; margin: .5rem 0; }
ol.ladder { list-style: none; padding: 0; }
ol.ladder li { border: 1px solid #e5e5e5; border-radius: 8px; padding: .75rem 1rem; margin-bottom: .6rem; background: #fff; }
.rung-title { font-weight: 600; }
.badge { float: right; font-size: .72rem; text-transform: uppercase; letter-spacing: .04em;
  padding: .15rem .5rem; border-radius: 999px; background: #eee; color: #555; }
.badge.complete { background: #d5f5e3; color: #1e7e46; }
.badge.active { background: #e8e3ff; color: #5a4bd6; }
.badge.built { background: #fdf0d5; color: #96690a; }
.rung-learn { color: #444; font-size: .92rem; margin-top: .25rem; }
.rung-done { color: #777; font-size: .85rem; margin-top: .15rem; }
ul.progress, ul.teachbacks { padding-left: 1.1rem; }
.teachbacks li { margin-bottom: .9rem; }
.verdict-pass { color: #1e7e46; font-weight: 600; }
.verdict-fail { color: #b03a2e; font-weight: 600; }
figure { margin: 1rem 0; }
figcaption { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: .82rem; color: #555; margin-bottom: .3rem; }
pre { background: #1e1e2e; color: #e4e4ef; padding: 1rem; border-radius: 8px; overflow-x: auto;
  font: .85rem/1.5 ui-monospace, "SF Mono", Menlo, monospace; }
img.asset { max-width: 100%; border: 1px solid #e5e5e5; border-radius: 8px; }
iframe.preview { width: 100%; height: 420px; border: 1px solid #e5e5e5; border-radius: 8px; background: #fff; }
footer { margin-top: 2.5rem; padding-top: 1rem; border-top: 1px solid #e5e5e5; color: #888; font-size: .82rem; }
"""


def export_session_html(session: Session) -> str:
    """Render a whole learning session as one self-contained HTML document.

    Args:
        session: The session to export (its mission, ladder, progress,
            teach-backs, and the files in its workspace).

    Returns:
        A complete ``<!doctype html>`` string with all assets inlined — safe to
        write to a ``.html`` file and open offline.
    """
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>ForgeLearn — {escape(session.topic or session.mission or session.id)}</title>",
        f"<style>{_STYLE}</style>",
        "</head><body>",
        _header(session),
        _mission_section(session),
        _ladder_section(session),
        _progress_section(session),
        _teachbacks_section(session),
        _files_section(session),
        _footer(session),
        "</body></html>",
    ]
    html = "\n".join(p for p in parts if p)
    _logger.info("exported session %s (%d chars)", session.id, len(html))
    return html


# --- Sections ---------------------------------------------------------------


def _header(session: Session) -> str:
    """The page banner: product name, topic, and creation date."""
    topic = escape(session.topic) if session.topic else "your learning journey"
    return (
        "<header>"
        f'<h1><span class="tag">ForgeLearn</span> — {topic}</h1>'
        f'<div class="muted">Started {session.created_at.date().isoformat()} · '
        f"session {escape(session.id)}</div>"
        "</header>"
    )


def _mission_section(session: Session) -> str:
    """The mission statement, if one was generated."""
    if not session.mission:
        return ""
    return f'<h2>Mission</h2><p class="mission">{escape(session.mission)}</p>'


def _ladder_section(session: Session) -> str:
    """The ladder of rungs with their status, what/why/done-when."""
    if not session.projects:
        return ""
    rows = ["<h2>The ladder</h2><ol class='ladder'>"]
    for index, project in enumerate(session.projects, start=1):
        badge = _STATUS_LABEL.get(project.status, project.status.value)
        rows.append(
            "<li>"
            f'<span class="badge {project.status.value}">{escape(badge)}</span>'
            f'<div class="rung-title">{index}. {escape(project.you_build)}</div>'
            f'<div class="rung-learn">Learn: {escape(project.you_learn)}</div>'
            f'<div class="rung-done">Done when: {escape(project.done_when)}</div>'
            "</li>"
        )
    rows.append("</ol>")
    return "".join(rows)


def _progress_section(session: Session) -> str:
    """The dated progress log, compared only against day one."""
    if not session.progress:
        return ""
    items = "".join(
        f"<li>{entry.on.isoformat()} — {escape(entry.note)}</li>"
        for entry in session.progress
    )
    return f"<h2>Progress vs day one</h2><ul class='progress'>{items}</ul>"


def _teachbacks_section(session: Session) -> str:
    """The teach-back record: what the learner explained and the verdict."""
    if not session.teachbacks:
        return ""
    items = []
    for tb in session.teachbacks:
        verdict = (
            '<span class="verdict-pass">passed</span>'
            if tb.passed
            else '<span class="verdict-fail">not yet</span>'
        )
        feedback = f" — {escape(tb.feedback)}" if tb.feedback else ""
        items.append(
            f"<li><strong>{escape(tb.project_id)}</strong>: {verdict}{feedback}"
            f'<div class="muted">You said: {escape(tb.explanation)}</div></li>'
        )
    return f"<h2>Teach-backs</h2><ul class='teachbacks'>{''.join(items)}</ul>"


def _files_section(session: Session) -> str:
    """Inline every workspace file: text in <pre>, images as data: URIs."""
    try:
        entries = list_files(session.id)
    except Exception as exc:  # noqa: BLE001 — export is best-effort about the workspace
        _logger.warning("could not list workspace for %s: %s", session.id, exc)
        entries = []
    if not entries:
        return "<h2>What you built</h2><p class='muted'>No project files were saved for this session.</p>"

    blocks = ["<h2>What you built</h2>"]
    for entry in entries:
        blocks.append(_file_block(session.id, entry.path, entry.size))
    return "".join(blocks)


def _footer(session: Session) -> str:
    """A small provenance line at the bottom of the export."""
    return (
        "<footer>Exported from ForgeLearn — you learned this by building it. "
        "This file is self-contained and works offline.</footer>"
    )


# --- One file's inlined block ------------------------------------------------


def _file_block(session_id: str, rel_path: str, size: int) -> str:
    """Render one workspace file as an inlined figure (text, image, or note)."""
    suffix = _suffix(rel_path)
    try:
        raw = read_bytes(session_id, rel_path)
    except Exception as exc:  # noqa: BLE001 — one unreadable file must not break export
        _logger.warning("skipping unreadable export file %s: %s", rel_path, exc)
        return (
            f'<figure><figcaption>{escape(rel_path)}</figcaption>'
            "<p class='muted'>could not read this file</p></figure>"
        )

    caption = f"<figcaption>{escape(rel_path)}</figcaption>"

    if suffix in _IMAGE_MIME:
        data_uri = _data_uri(_IMAGE_MIME[suffix], raw)
        return f'<figure>{caption}<img class="asset" src="{data_uri}" alt="{escape(rel_path)}"></figure>'

    text = _decode_text(raw)
    if text is None:  # opaque binary we won't inline as text
        return f"<figure>{caption}<p class='muted'>binary file, {size} bytes</p></figure>"

    # A browser toy: preview its markup, sandboxed with no scripts (JS won't run;
    # the source below stays the source of truth).
    preview = ""
    if _basename(rel_path).lower() in _LIVE_PREVIEW_NAMES:
        preview = f'<iframe class="preview" sandbox srcdoc="{escape(text, quote=True)}"></iframe>'

    return f"<figure>{caption}{preview}<pre>{escape(text)}</pre></figure>"


# --- Small pure helpers ------------------------------------------------------


def _suffix(path: str) -> str:
    """Lowercased file extension including the dot, or '' if none."""
    base = _basename(path)
    return base[base.rfind(".") :].lower() if "." in base else ""


def _basename(path: str) -> str:
    """The final path segment of a ``/``-separated workspace path."""
    return path.rsplit("/", 1)[-1]


def _decode_text(raw: bytes) -> str | None:
    """Decode bytes as UTF-8 text, or None if they aren't valid text."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _data_uri(mime: str, raw: bytes) -> str:
    """Build a ``data:`` URI embedding ``raw`` as base64 for offline use."""
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{encoded}"
