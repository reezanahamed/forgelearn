"""Per-session project workspaces — where the agent's real files live and run.

Each browser session owns one workspace directory under the configured
``workspace_dir``. The agent writes real code into it, the browser lists its
files and reads them, and the Run action executes the project as a subprocess.

Two responsibilities, two modules:

* :mod:`forgelearn.workspace.manager` — locate/create a session's workspace and
  inspect it (list files, read a file, resolve the Run entrypoint). All path
  handling is confined here and hardened against traversal.
* :mod:`forgelearn.workspace.runner` — run the resolved entrypoint as a subprocess
  and stream its output as normalized :class:`~forgelearn.common.types.AgentEvent`
  objects, reusing the same event vocabulary the agent stream uses.
"""

from __future__ import annotations

from forgelearn.workspace.manager import (
    SESSION_PREFIX,
    FileEntry,
    create_ephemeral,
    find_entrypoint,
    get_or_create,
    list_files,
    read_bytes,
    read_file,
    workspace_path,
)
from forgelearn.workspace.runner import run_workspace

__all__ = [
    "SESSION_PREFIX",
    "FileEntry",
    "create_ephemeral",
    "find_entrypoint",
    "get_or_create",
    "list_files",
    "read_bytes",
    "read_file",
    "run_workspace",
    "workspace_path",
]
