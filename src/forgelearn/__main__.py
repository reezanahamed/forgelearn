"""Enable ``python -m forgelearn`` to run the same entrypoint as the CLI."""

from __future__ import annotations

from forgelearn.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
