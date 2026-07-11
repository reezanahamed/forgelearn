"""End-to-end check for the Codex adapter against a Codex-wire fixture CLI.

The real ``codex`` binary isn't installed in every environment (and needs paid
auth), so this test points the adapter's configured CLI command at a tiny local
program that emits *real* ``codex exec --json`` JSONL and actually writes a file.
That still exercises the full second-provider path for real — a spawned
subprocess, line-by-line streaming through :mod:`forgelearn.agents.process`, and
Codex-schema normalization — and asserts BOTH that the steps stream AND that the
file lands, which is Phase 6's "a second agent builds the same task" bar.

It runs offline and fast (no network, no tokens), so it is not gated.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from forgelearn.agents import CodexAgent
from forgelearn.common.types import EventKind
from forgelearn.config import Settings

# A minimal `codex exec --json` emulator: it ignores its flags, writes hello.py
# into its working directory (which the adapter sets to the workspace), and
# streams the exact Codex JSONL event shapes the normalizer maps.
_FIXTURE_CODEX = """#!/usr/bin/env bash
set -e
echo '{"type":"thread.started","thread_id":"t-fixture"}'
echo '{"type":"turn.started"}'
echo '{"type":"item.completed","item":{"id":"i0","type":"agent_message","text":"Creating hello.py"}}'
printf 'print("hello from codex")\\n' > hello.py
echo '{"type":"item.completed","item":{"id":"i1","type":"file_change","changes":[{"path":"hello.py","kind":"add"}],"status":"completed"}}'
echo '{"type":"turn.completed","usage":{"output_tokens":5}}'
"""

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="fixture CLI needs bash"
)


def test_codex_adapter_builds_a_real_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive the Codex adapter against a fixture CLI: steps stream, file lands."""
    fixture = tmp_path / "fake-codex"
    fixture.write_text(_FIXTURE_CODEX, encoding="utf-8")
    fixture.chmod(0o755)

    workspace = tmp_path / "session"
    workspace.mkdir()

    tuned = Settings(codex_cli_command=str(fixture))
    monkeypatch.setattr("forgelearn.agents.codex.get_settings", lambda: tuned)

    events = list(CodexAgent().run_events("Create hello.py", workspace))

    # The adapter streamed the agent's steps, normalized to our vocabulary...
    kinds = {e.kind for e in events}
    assert EventKind.NARRATION in kinds, "expected narration"
    assert EventKind.FILE_WRITE in kinds, "expected a file-write event"
    assert events[-1].kind == EventKind.DONE, "expected a terminal DONE"
    assert any(e.path == "hello.py" for e in events), "file-write should surface the path"

    # ...and the real file the fixture wrote appeared in the workspace.
    hello = workspace / "hello.py"
    assert hello.is_file(), f"adapter run did not produce {hello}"
    assert "hello from codex" in hello.read_text()
