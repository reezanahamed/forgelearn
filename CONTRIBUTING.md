# Contributing to ForgeLearn

Thanks for trying ForgeLearn! It's an early project, and the most valuable thing you can give right now is **honest feedback from actually using it**: what felt great, what broke, what confused you. This guide is mostly for feedback-givers, with a short section for code contributors at the end.

## Ways to help (easiest first)

1. **Use it and tell us how it felt.** Learn something real with it, then open an issue describing the experience, the good and the bad.
2. **Report bugs** when something breaks or behaves wrong.
3. **Suggest improvements**: a rough edge, a missing feature, a confusing moment.
4. **Contribute code** via a pull request.

## Set up to try it

To just try it, clone this repo. To contribute code, fork it first and clone your fork (replace `<your-username>`):

```bash
# try it
git clone https://github.com/reezanahamed/forgelearn.git && cd forgelearn
# or, to contribute: git clone https://github.com/<your-username>/forgelearn.git && cd forgelearn
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
forgelearn        # open http://localhost:8000
```

On Ubuntu/Debian, use `python3` (not `python`), and if `venv` fails with "ensurepip is not available", run `sudo apt install python3-venv` first. The [README](README.md#install) also documents a [uv](https://docs.astral.sh/uv/)-based install that avoids these system-Python issues.

You need at least one headless coding-agent CLI installed and authenticated, `claude` (Claude Code) or `codex` (OpenAI Codex), for the live "Build" step. Everything else (interview, ladder, resume, export) works regardless.

## Filing a good bug report

A report we can act on beats a detailed guess. Please include:

- **What you did**: the steps, starting from `forgelearn` (e.g. topic you typed, which button you pressed).
- **What you expected** vs. **what actually happened.**
- **Environment**: your OS, Python version (`python --version`), and which agent CLI you used and its version (`claude --version` / `codex --version`).
- **Server logs**: the relevant lines the `forgelearn` process printed in your terminal around the failure.
- **A screenshot** if it's a UI issue.

> ⚠️ Please **don't paste API keys, `.env` contents, or anything private** into an issue. Redact them first.

## Suggesting a feature

Describe **the problem you hit**, not just the solution you have in mind. The underlying need often has a better fix than the first idea. Say who it helps and when it matters.

## Running the tests

```bash
pytest
```

The suite is fast and fully offline. It fakes the agent, so nothing spawns a real CLI or calls a model. There is one **opt-in** live test that drives the real `claude` CLI; it only runs when you ask for it:

```bash
FORGELEARN_RUN_AGENT_TESTS=1 pytest
```

## Contributing code

Keep changes small and focused (one idea per PR). To match the codebase:

- **Modular.** One responsibility per module; shared logic goes in `src/forgelearn/common/`.
- **Central config.** Settings, ports, model names, timeouts live only in `src/forgelearn/config.py` (imported everywhere). No scattered magic numbers.
- **Type hints and docstrings** on functions; log via the shared logger in `common/logging.py` (no bare `print`); raise the custom errors in `common/errors.py`.
- **Add at least one test** for what you change, and run `pytest` before opening the PR.

Then open a pull request describing what changed and why.

## A note on safety

The **Run** button executes the project's code as a subprocess with your own permissions. That's fine for local, single-user use, but **don't run code from an untrusted session**, and treat sandboxing as a prerequisite before exposing ForgeLearn to multiple users.

For **security issues**, please don't post exploit details in a public issue. Use GitHub's private vulnerability reporting (Security, then *Report a vulnerability*), or contact the maintainer privately.

## Be kind

Assume good faith, keep feedback respectful and specific, and remember there's a person on the other end. Thanks for helping make ForgeLearn better. 💛
