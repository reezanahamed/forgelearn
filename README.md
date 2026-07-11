# ForgeLearn

**ForgeLearn teaches you any subject by building it in front of you, then making you prove you learned it.**

The AI writes and runs real, editable code in your browser: actual files and a Run button, not just chat about code. You cannot move on to the next project until you explain the last one back in your own words, which is the teach-back gate. And it is not only for coding: when a topic can't be an app, it builds an interactive simulation or visual you can poke at instead.

## What it does

You type a topic. The AI interviews you, generates a ladder of tiny projects sized to what you already know, and builds the first one live (real files you can Run) while it teaches. When a project's *done-when* passes, it runs a teach-back before the next rung unlocks. Two swappable engines (Claude Code, OpenAI Codex) sit behind one interface, chosen from a dropdown. Sessions are **saved to disk**, so a returning learner **resumes where they left off**, and any session **exports to a single self-contained HTML file** that works offline. Once it's running, the learning happens entirely in the browser, no terminal needed. One command installs it, one command runs it.

> Early project. Feedback and issues welcome.

## Install

Needs **Python 3.10 or newer** and at least one headless coding-agent CLI installed and authenticated: `claude` (Claude Code) by default, or `codex` (OpenAI Codex).

### Option A: with uv (recommended)

[uv](https://docs.astral.sh/uv/) manages the virtual environment and Python for you, so it avoids the common Ubuntu/Debian `python` and `ensurepip` errors below.

```bash
# install uv once (other methods: https://docs.astral.sh/uv/getting-started/installation/)
curl -LsSf https://astral.sh/uv/install.sh | sh

git clone https://github.com/reezanahamed/forgelearn.git && cd forgelearn
uv venv
uv pip install -e ".[dev]"
uv run forgelearn        # starts the server on http://localhost:8000
```

### Option B: with python and venv

Use `python3` (on Ubuntu/Debian `python` often isn't installed). If `venv` fails with *"ensurepip is not available"*, install the venv package first:

```bash
sudo apt install python3-venv        # Debian/Ubuntu only, if the next line fails

git clone https://github.com/reezanahamed/forgelearn.git && cd forgelearn
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
forgelearn               # starts the server on http://localhost:8000
```

## Run

Once installed, start the server (`forgelearn`, or `uv run forgelearn` if you used uv without activating the venv), then open http://localhost:8000. Type what you want to learn and follow the flow. From here on it is all in the browser.

## Switch the AI (provider)

ForgeLearn drives a **headless CLI coding agent** as its engine, and the provider is swappable two ways:

- **Per build, in the browser.** A dropdown next to the composer picks which installed agent (`claude` / `codex`) builds the current rung.
- **Defaults, in config.** Set `FORGELEARN_DEFAULT_AGENT` (the fallback provider) and `FORGELEARN_ORCHESTRATOR_AGENT` (which provider runs the interview/ladder/teach-back reasoning). Point a provider at a different binary or model with `FORGELEARN_AGENT_CLI_COMMAND` / `FORGELEARN_AGENT_MODEL` (Claude) or `FORGELEARN_CODEX_CLI_COMMAND` / `FORGELEARN_CODEX_MODEL` (Codex).

Each selectable provider's CLI must be installed and authenticated on the host.

## Resume & export

- **Resume.** Every session is persisted to `sessions/` (`FORGELEARN_SESSIONS_DIR`) as JSON. Reopen the page and your last session (mission, ladder, progress) loads automatically. It also survives a server restart.
- **Export.** Press **Export as HTML** in the ladder rail to download the whole session as one self-contained `.html` file: mission, ladder, progress, teach-backs, and every file you built (images inlined as `data:` URIs). It opens offline with no server.

## Configure

Copy `.env.example` to `.env` and edit. Every setting has a default (see `src/forgelearn/config.py`); host environment variables override the `.env` file. All variables use the `FORGELEARN_` prefix, e.g. `FORGELEARN_PORT=9000`.

## Run with Docker (optional)

```bash
docker build -t forgelearn .
docker run -p 8000:8000 -e ANTHROPIC_API_KEY=your-key \
  -v "$PWD/sessions:/app/sessions" -v "$PWD/workspaces:/app/workspaces" forgelearn
```

The image serves everything on one port. Note: it does **not** bundle an agent CLI. To run the live build step, make an authenticated `claude`/`codex` CLI reachable inside the container (install it in a derived image or mount it). The mounted volumes keep your sessions and built projects across restarts.

## Test

```bash
pytest
```

## Layout

```
forgelearn/
  src/forgelearn/
    config.py            # central config, the ONLY place for settings/constants
    cli.py               # `forgelearn` entrypoint (starts the server)
    common/              # DRY shared code: logging, errors, types
    agents/              # swappable headless CLI engines (claude, codex)
    server/              # FastAPI app: UI + API + SSE, and the learning routes
    workspace/           # per-session project folders + Run
    orchestrator/        # the learning method: interview, ladder, build, teach-back
    storage/             # durable session store (JSON) + self-contained HTML export
    frontend/            # static browser UI served by the server
  tests/
```

## Contributing & feedback

This is an early project and feedback is the most useful thing you can give. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for how to report bugs, suggest features, or send a pull request.

## Credits

Built with [Claude Code](https://claude.com/claude-code).
