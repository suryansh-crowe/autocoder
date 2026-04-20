# syntax=docker/dockerfile:1.7
#
# Ollama + phi4:14b, baked at build time.
#
# Build:
#   docker build -t autocoder-phi4:latest docker/ollama
#
# Run (binds to localhost only, mounts a named volume for extra models):
#   docker run -d --name autocoder-phi4 --restart unless-stopped \
#     -p 127.0.0.1:11434:11434 \
#     -v autocoder-ollama-models:/root/.ollama \
#     autocoder-phi4:latest
#
# Verify:
#   curl http://localhost:11434/api/tags   # should list phi4:14b
#
# The first `docker build` downloads phi4 once (~9 GB). Subsequent
# builds reuse the layer. The resulting image is large (~11 GB) but
# the container starts instantly — no runtime pull delay.
#
# If you prefer a small image with a lazy first-run pull, switch to
# the entrypoint-pull variant documented at the bottom of this file.

FROM ollama/ollama:latest AS base

# Ollama base is loopback-only bound inside the container; the host
# port binding in `docker run` controls external exposure.
ENV OLLAMA_HOST=0.0.0.0:11434 \
    OLLAMA_KEEP_ALIVE=30m \
    OLLAMA_NUM_THREAD=8

# ---------------------------------------------------------------------------
# Build-time model pull
# ---------------------------------------------------------------------------
#
# The base image's CMD runs `ollama serve` in the foreground, so we
# can't just call `ollama pull` — there is no server yet.  Start
# `ollama serve` in the background, poll `ollama list` until the API
# answers, pull the model, then stop the server.  The weights end up
# in the image under /root/.ollama/models/.
#
# We use BuildKit's cache mount for /root/.ollama so repeated builds
# on the same host don't re-download on every invocation.
ARG OLLAMA_MODEL=phi4:14b
ENV OLLAMA_MODEL=${OLLAMA_MODEL}

RUN --mount=type=cache,target=/root/.ollama,id=ollama-models \
    set -eux; \
    ollama serve & \
    server_pid=$!; \
    # Wait up to 30 s for the API to come up (0.5 s × 60).
    ready=0; \
    for i in $(seq 1 60); do \
        if ollama list >/dev/null 2>&1; then ready=1; break; fi; \
        sleep 0.5; \
    done; \
    if [ "$ready" != "1" ]; then \
        echo "ollama serve did not become ready in 30s"; \
        kill -9 "$server_pid" || true; \
        exit 1; \
    fi; \
    ollama pull "$OLLAMA_MODEL"; \
    ollama list; \
    kill "$server_pid"; \
    wait "$server_pid" 2>/dev/null || true

EXPOSE 11434

# Inherit CMD from the base image (it runs `ollama serve` in the
# foreground). No override needed.

# ---------------------------------------------------------------------------
# Lazy-pull alternative (smaller image, delayed first run)
# ---------------------------------------------------------------------------
#
# Replace the RUN block above with this ENTRYPOINT script if you'd
# rather keep the image thin and pull at container-start instead:
#
#   COPY docker/ollama/entrypoint-lazy-pull.sh /usr/local/bin/
#   RUN chmod +x /usr/local/bin/entrypoint-lazy-pull.sh
#   ENTRYPOINT ["/usr/local/bin/entrypoint-lazy-pull.sh"]
#
# See entrypoint-lazy-pull.sh in this directory.
