"""Central configuration for ForgeLearn.

This module is the ONE place where settings, ports, paths, model names, and
timeouts live. Every other module imports from here — no scattered magic
numbers or hard-coded strings elsewhere in the codebase.

Settings are loaded (in increasing order of precedence) from field defaults,
a local ``.env`` file, then host environment variables. All variables use the
``FORGELEARN_`` prefix, e.g. ``FORGELEARN_PORT=9000``.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# The platform root (the directory that holds pyproject.toml / .env). This file
# lives at <root>/src/forgelearn/config.py, so the root is three parents up.
PLATFORM_ROOT: Path = Path(__file__).resolve().parents[2]

# The installed package directory (<root>/src/forgelearn). Code-adjacent assets
# such as the static frontend live under here, so they travel with the package
# regardless of the working directory the server is launched from.
PACKAGE_ROOT: Path = Path(__file__).resolve().parent


class Settings(BaseSettings):
    """Typed, validated application settings.

    Attributes:
        host: Interface the FastAPI server binds to.
        port: TCP port the single server listens on (UI + API + orchestrator).
        default_agent: Name of the headless CLI coding agent the backend drives.
        workspace_dir: Directory holding per-session project workspaces.
        sessions_dir: Directory holding persisted session/progress records.
        frontend_dir: Directory holding the static browser UI the server serves.
        log_level: Root logging level for the shared logger.
        run_timeout_seconds: Wall-clock limit for one Run of a user's project.
        run_entrypoints: Priority filenames tried first as the Run entrypoint.
        workspace_max_view_bytes: Byte cap for the browser file viewer.
    """

    model_config = SettingsConfigDict(
        env_prefix="FORGELEARN_",
        env_file=str(PLATFORM_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = "127.0.0.1"
    port: int = 8000
    default_agent: str = "claude"
    workspace_dir: Path = Field(default=Path("workspaces"))
    sessions_dir: Path = Field(default=Path("sessions"))
    # Ships inside the package; anchored to PACKAGE_ROOT, not the platform root,
    # so the UI is found no matter where `forgelearn` is launched from.
    frontend_dir: Path = Field(default=PACKAGE_ROOT / "frontend")
    log_level: str = "INFO"

    # --- Agent engine (the headless CLI coding agents the backend drives) ---
    # ForgeLearn drives one of several swappable CLI agents (Phase 6). Two tunables
    # are SHARED by every provider; the rest are per-provider. The fixed
    # stream/JSON protocol flags are NOT here — they are constants in each
    # adapter (agents/claude.py, agents/codex.py) because changing them breaks
    # that provider's parser.
    #
    # Which provider a request uses is chosen by the browser dropdown (falling
    # back to ``default_agent``); ``get_agent(name)`` maps the name to the adapter.

    # Shared across all providers:
    agent_timeout_seconds: int = 300  # hard wall-clock limit for one agent run
    # Local single-user MVP posture: the agent may write/run real code without
    # per-action prompts. Each adapter maps this to its own bypass flag. Safe
    # sandboxing of arbitrary code is deferred (PLAN §10).
    agent_skip_permissions: bool = True

    # Claude Code (``claude``) provider:
    agent_cli_command: str = "claude"
    agent_model: str | None = None  # None -> let the CLI pick its default model
    # Comma/space-separated allow-list (e.g. "Write,Edit,Bash"); empty omits the
    # flag. Ignored in practice when agent_skip_permissions is True.
    agent_allowed_tools: str = ""
    # --bare skips local hooks/skills/CLAUDE.md for reproducibility, but forces
    # API-key auth (no OAuth/keychain). Off by default so subscription auth works.
    agent_bare: bool = False

    # OpenAI Codex (``codex``) provider:
    codex_cli_command: str = "codex"
    codex_model: str | None = None  # None -> let the CLI pick its default model

    # --- Orchestrator (Phase 7: the learning method — the core IP) ---
    # The teaching state machine (interview → ladder → build → teach-back). These
    # shape the METHOD, not the transport; the prompts that carry the teaching
    # principles live in orchestrator/prompts.py, not here.
    #
    # Which provider answers the orchestrator's own questions (interview, ladder,
    # teach-back judging). None → fall back to ``default_agent`` so a single-key
    # setup just works. It need not match the provider that BUILDS a project.
    orchestrator_agent: str | None = None
    # The interview is adaptive: the tutor asks between these many questions before
    # generating a mission — enough to gauge the learner's PURPOSE/depth (casual,
    # academic, career, research), their current KNOWLEDGE LEVEL (for ZPD sizing,
    # TEACHING_PRINCIPLES #9), and time available, but no more than needed. It need
    # not be a fixed three (PLAN §3 lists why / what-you-know / hours as a floor).
    interview_min_questions: int = 3
    interview_max_questions: int = 6
    # A ladder holds between this many rungs — enough to reach the goal, few
    # enough that each is a short single-win project (TEACHING_PRINCIPLES #10).
    ladder_min_projects: int = 5
    ladder_max_projects: int = 7
    # The teach-back gate may ask up to this many probing follow-ups before it
    # judges (retrieval practice + desirable difficulty; TEACHING_PRINCIPLES #3/#6).
    teachback_max_probes: int = 3
    # Default school grade the AI writes for (plain, simple English). The browser
    # dropdown can override it per session; lower = simpler wording.
    reading_grade_default: int = 7

    # --- Workspace + Run (Phase 5: run a built project from the browser) ---
    # Hard wall-clock timeout for one Run of a user's project, in seconds. Kept
    # short: a Run is meant to be a quick "does it work", not a long job.
    run_timeout_seconds: int = 60
    # Priority list (comma/space-separated) of filenames tried first as the Run
    # entrypoint; the runner falls back to the sole/newest .py file if none match.
    run_entrypoints: str = "main.py,app.py"
    # Cap on how many bytes of a workspace file the browser file viewer will read,
    # so opening a huge/binary file can't exhaust memory or flood the response.
    workspace_max_view_bytes: int = 200_000

    @field_validator("workspace_dir", "sessions_dir")
    @classmethod
    def _resolve_relative_to_root(cls, value: Path) -> Path:
        """Anchor relative storage paths to the platform root, keep absolutes."""
        return value if value.is_absolute() else (PLATFORM_ROOT / value)

    @field_validator("frontend_dir")
    @classmethod
    def _resolve_relative_to_package(cls, value: Path) -> Path:
        """Anchor a relative frontend dir to the package, keep absolutes."""
        return value if value.is_absolute() else (PACKAGE_ROOT / value)

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        """Uppercase the log level so ``info`` and ``INFO`` behave the same."""
        return value.upper()

    @model_validator(mode="after")
    def _order_range_bounds(self) -> "Settings":
        """Keep min/max bounds consistent so a misconfig can't invert a range."""
        if self.interview_max_questions < self.interview_min_questions:
            self.interview_max_questions = self.interview_min_questions
        if self.ladder_max_projects < self.ladder_min_projects:
            self.ladder_max_projects = self.ladder_min_projects
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so every module shares one immutable configuration instance.

    Returns:
        The loaded :class:`Settings`.
    """
    return Settings()
