#!/bin/sh
# Start `ollama serve` in the foreground. Before that, make sure
# ${OLLAMA_MODEL:-phi4:14b} is present in the local model store —
# pull it once if this is the first container start.
#
# Used by the lazy-pull Dockerfile variant. See Dockerfile for the
# baked-at-build-time variant.

set -eu

MODEL="${OLLAMA_MODEL:-phi4:14b}"

# Kick serve into the background so we can query /api/tags.
ollama serve &
server_pid=$!

# Wait up to 30 s for the API to respond.
ready=0
for _ in $(seq 1 60); do
    if ollama list >/dev/null 2>&1; then
        ready=1
        break
    fi
    sleep 0.5
done
if [ "$ready" != "1" ]; then
    echo "[entrypoint] ollama serve did not become ready in 30s" >&2
    kill -9 "$server_pid" || true
    exit 1
fi

# Pull only if the tag isn't already resident.
if ollama list | awk 'NR > 1 { print $1 }' | grep -Fxq "$MODEL"; then
    echo "[entrypoint] $MODEL already present — skipping pull"
else
    echo "[entrypoint] pulling $MODEL (this can take 5-20 min on first run)"
    ollama pull "$MODEL"
fi

# Hand control over to serve (which we started above). `wait` blocks
# until serve exits; container lifecycle = serve lifecycle.
wait "$server_pid"
