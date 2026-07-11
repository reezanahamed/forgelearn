# ForgeLearn container image (Phase 8, optional).
#
# Builds a single image that runs the whole platform — UI + API + orchestrator —
# on one port, exactly like `forgelearn` on a laptop. python:3.12-slim is the safe
# default base (glibc, so wheels/native extensions install cleanly).
#
# IMPORTANT: ForgeLearn's engine is a headless CLI coding agent (claude / codex).
# This image does NOT bundle those CLIs or their credentials. To actually build
# projects you must make an authenticated agent CLI available to the container
# (install it in a derived image, or mount it) and pass its key, e.g.
#   docker build -t forgelearn .
#   docker run -p 8000:8000 -e ANTHROPIC_API_KEY=… forgelearn
# The server, interview, ladder, teach-back, storage, and export all work without
# that; only the live build step needs a reachable agent CLI.

FROM python:3.12-slim

# Faster, quieter, unbuffered Python in a container.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Bind to all interfaces so the mapped port is reachable from the host.
    FORGELEARN_HOST=0.0.0.0 \
    FORGELEARN_PORT=8000

WORKDIR /app

# Copy metadata first so the dependency layer is cached across code changes.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

# Persisted sessions and per-session workspaces live here; mount a volume to keep
# them across container restarts.
VOLUME ["/app/sessions", "/app/workspaces"]

EXPOSE 8000

CMD ["forgelearn"]
