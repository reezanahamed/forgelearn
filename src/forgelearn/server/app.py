"""The FastAPI application — one server, one port (PLAN §8a).

A single process serves the browser UI, the JSON API, and the SSE agent stream
on one port; there is no separate frontend server. :func:`create_app` builds and
wires the app (so tests can drive it with a ``TestClient`` without binding a
socket), and :func:`run` boots it under uvicorn for the ``forgelearn`` command.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from forgelearn import __version__
from forgelearn.common.logging import get_logger
from forgelearn.config import get_settings
from forgelearn.server.learn import router as learn_router
from forgelearn.server.routes import router

_logger = get_logger("server.app")

# Sub-path under which static frontend assets (JS/CSS/images added in Phase 4)
# are served. The page itself is served at "/" by the index route.
_STATIC_MOUNT = "/static"
_STATIC_NAME = "static"


def create_app() -> FastAPI:
    """Build and configure the ForgeLearn FastAPI application.

    Registers the API/SSE/index routes and, if the configured frontend
    directory exists, mounts it for static assets. Kept free of any network
    binding so it is importable and testable in isolation.

    Returns:
        The configured :class:`FastAPI` instance.
    """
    app = FastAPI(
        title="ForgeLearn",
        version=__version__,
        summary="Learn any subject by building it, live, in the browser.",
    )

    @app.middleware("http")
    async def _no_cache_frontend(request, call_next):
        """Tell the browser never to cache the UI (index + static JS/CSS).

        ForgeLearn is a fast-moving local app updated by ``git pull``; without this
        the browser serves stale ``app.js``/``styles.css`` from cache and users
        keep seeing old behaviour after updating. The API is dynamic anyway.
        """
        response = await call_next(request)
        path = request.url.path
        if path == "/" or path.startswith(_STATIC_MOUNT):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

    app.include_router(router)
    app.include_router(learn_router)  # the Phase 7 learning-method API

    frontend_dir = get_settings().frontend_dir
    if frontend_dir.is_dir():
        # html=False: "/" is handled by the index route; this mount only serves
        # future asset files (Phase 4) under /static.
        app.mount(
            _STATIC_MOUNT,
            StaticFiles(directory=str(frontend_dir)),
            name=_STATIC_NAME,
        )
    else:  # pragma: no cover - defensive; the frontend ships with the package
        _logger.warning("frontend dir not found, static assets disabled: %s", frontend_dir)

    return app


def run() -> None:
    """Start the server under uvicorn on the configured host/port (blocking).

    This is the body of the ``forgelearn`` command: it stays running and serves
    the browser UI + API until interrupted (Ctrl+C).
    """
    import uvicorn  # local import so importing the app never pulls in the server

    settings = get_settings()
    _logger.info(
        "starting ForgeLearn server on http://%s:%s", settings.host, settings.port
    )
    uvicorn.run(
        create_app(),
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )
