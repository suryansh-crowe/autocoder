# autocoder — Ollama + phi4:14b image

Self-contained Docker setup that produces an Ollama server with
`phi4:14b` already loaded, bound to `127.0.0.1:11434`, ready for
`autocoder run` / `autocoder generate` to talk to.

## Prerequisites

- Docker Desktop (Windows/macOS) or Docker Engine 20.10+ (Linux)
  with **BuildKit enabled** (default in recent versions — the
  Dockerfile uses a cache mount that needs BuildKit).
- 12 GB free disk (≈ 9 GB for the model, plus image + base layers).
- Port `11434` free on the host. **If you have the Windows Ollama
  desktop app installed, it auto-starts on login and binds 11434.**
  Either stop it (`Get-Process ollama* | Stop-Process -Force` in
  PowerShell) or disable **Ollama** in *Task Manager → Startup
  apps* before running the commands below — otherwise
  `docker compose up` fails with
  `bind: Only one usage of each socket address (protocol/network address/port) is normally permitted`.

## Build + run

### Option A — docker compose (one command)

From the project root (not from inside `docker/ollama/`):

```bash
docker compose -f docker/ollama/docker-compose.yml up -d --build
```

That builds `autocoder-phi4:latest` (downloading `phi4:14b` into the
image if it isn't already), starts the container with the right
port binding + volume + env, and returns control. First build
takes **5-20 min** depending on bandwidth (pulling ~1.5 GB base +
~9 GB model). Subsequent rebuilds reuse the cached layer.

Wait for the healthcheck to flip to `healthy` (~20 s after
start) and confirm the API is live:

```bash
docker ps --filter name=autocoder-phi4 --format "{{.Names}} {{.Status}}"
# → autocoder-phi4  Up 42 seconds (healthy)

curl http://localhost:11434/api/tags
# → {"models":[{"name":"phi4:14b","model":"phi4:14b",
#      "parameter_size":"14.7B","quantization_level":"Q4_K_M",...}]}
```

Stop / restart / tail:

```bash
docker compose -f docker/ollama/docker-compose.yml stop
docker compose -f docker/ollama/docker-compose.yml start
docker compose -f docker/ollama/docker-compose.yml logs -f
```

### Option B — plain docker

```bash
docker build -t autocoder-phi4:latest docker/ollama

docker run -d --name autocoder-phi4 --restart unless-stopped \
  -p 127.0.0.1:11434:11434 \
  -v autocoder-ollama-models:/root/.ollama \
  -e OLLAMA_KEEP_ALIVE=30m -e OLLAMA_NUM_THREAD=8 \
  autocoder-phi4:latest
```

## Wire the autocoder CLI to it

Your `.env` should set the backend to Ollama and point at localhost:

```env
USE_AZURE_OPENAI=false
OLLAMA_ENDPOINT=http://localhost:11434
OLLAMA_MODEL=phi4:14b
```

Then run as usual:

```bash
autocoder run --urls-file urls.txt
```

The autocoder's Ollama client streams responses, so the CPU-bound
Phi-4 (2-4 tok/s) doesn't trip HTTP idle timeouts.

## Build-time vs. lazy-pull

The default `Dockerfile` pulls `phi4:14b` **at build time** — the
image is ~11 GB, but container startup is instant because the model
weights are already inside the image.

If you'd rather keep the image thin (~1.5 GB) and pull on first
container start (adds ~5-20 min to the first `docker run`),
`entrypoint-lazy-pull.sh` is provided. Swap the build-time `RUN`
block in the Dockerfile for the `COPY` + `ENTRYPOINT` lines
commented at the bottom of that file.

## Switching to a different model

Either build arg or env override works.

Build a different baked-in model:

```bash
docker build --build-arg OLLAMA_MODEL=llama3.1:8b \
             -t autocoder-llama:latest docker/ollama
```

Or pull an additional model into a running container:

```bash
docker exec -it autocoder-phi4 ollama pull mistral:7b
```

The named volume (`autocoder-ollama-models`) preserves pulled models
across container rebuilds — you don't have to re-download when the
image updates.

## Platform notes

- **Windows (Docker Desktop / WSL2)**: make sure WSL memory is raised
  in `%USERPROFILE%\.wslconfig` to at least 16 GB — Phi-4 14B needs
  ~9 GB resident.
- **Apple Silicon**: works but CPU-only. For GPU inference on an
  Apple Silicon Mac, run Ollama natively instead of in Docker; the
  macOS host port binding gives you the same endpoint.
- **Linux with NVIDIA GPU**: add `--gpus=all` to the `docker run`
  (or the `deploy.resources.reservations.devices` block in
  compose) and Ollama will offload to GPU automatically.

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `bind: Only one usage of each socket address` | Windows Ollama desktop app already holds 11434 | `Get-Process ollama* \| Stop-Process -Force` then `docker compose … up -d`. Disable the **Ollama** entry in *Task Manager → Startup apps* to prevent recurrence. |
| `Cannot connect to the Docker daemon` | Docker Desktop not running | Start Docker Desktop; wait for the whale icon to say *Running*. |
| Build step #7 fails with `pulling fd7…: connection refused` | BuildKit cache mount issue, or `ollama serve` didn't start inside the RUN | Ensure BuildKit is enabled (`DOCKER_BUILDKIT=1` on older Linux installs). Retry; the partial model layers are cached. |
| `OCI runtime exec failed` on healthcheck | Container is still loading the model into RAM | Wait ~20 s after start; the healthcheck flips to `healthy` once `ollama list` answers. |
| `autocoder run` logs `ollama_http_error err=timed out` | Cold model load exceeded the idle-read timeout | Raise `OLLAMA_TIMEOUT_SECONDS=1200` in `.env`. The client streams responses, so this only matters for the very first request after a container start. |
| Pull stalls or hangs during build | Corporate TLS-inspecting proxy | Inject host root CAs into the container: `docker exec -it autocoder-phi4 bash -c 'cp /host-ca.crt /usr/local/share/ca-certificates/ && update-ca-certificates'` then retry. |
| Container memory pressure / OOM kills on Windows | WSL2 memory cap too low | Raise `memory` to 16 GB+ in `%USERPROFILE%\.wslconfig` and restart Docker Desktop. |

## Uninstall

```bash
docker compose -f docker/ollama/docker-compose.yml down
docker image rm autocoder-phi4:latest
docker volume rm autocoder-ollama-models    # deletes the ~9 GB model
```

## What actually runs during `docker build`

For the curious: the tricky part of building an Ollama image with
a model pre-loaded is that `ollama pull` needs the Ollama server
running — so the Dockerfile starts `ollama serve` in the
background, polls `ollama list` until the API answers, pulls the
model, then stops the server. The weights end up in
`/root/.ollama/models/` and are committed as an image layer. When
you later `docker run` the image, the base image's `CMD` (which
is `ollama serve`) starts up and the model is already resident.

The `RUN` block uses a BuildKit cache mount
(`--mount=type=cache,target=/root/.ollama,id=ollama-models`) so
repeated builds on the same host reuse previously-downloaded
weights instead of re-pulling. That's different from the runtime
named volume (`autocoder-ollama-models`) in the compose file —
the build cache is a BuildKit concept and lives in the Docker
daemon's storage driver, not in a mountable volume.
