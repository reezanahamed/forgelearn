"""The ForgeLearn web server — FastAPI app serving UI + API on one port (Phase 3).

One process serves the browser frontend, the JSON API, and the SSE stream that
pushes live agent activity to the page. :func:`create_app` returns the wired
application; :func:`run` boots it under uvicorn for the ``forgelearn`` command.
"""

from __future__ import annotations

from forgelearn.server.app import create_app, run

__all__ = ["create_app", "run"]
