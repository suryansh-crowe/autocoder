# 09 · LLM intake and call structure

The LLM does one job: produce a small JSON action plan. Two calls per
URL, both constrained, both validated. No Python code is ever written
by the model.

## Model and runtime

- **Model:** `phi4:14b` (Microsoft Phi-4, 14.7 B params, Q4_K_M).
- **Runtime:** Ollama, served at `http://localhost:11434`, CPU-only.
- **Why Phi-4 14B:** strongest instruction-following per GB of RAM
  among non-Chinese models that fit a 32 GB laptop. ~9 GB resident at
  Q4_K_M. ~2-4 tok/s on CPU is acceptable because plan outputs are
  ~50-200 tokens.

Swap the model with `OLLAMA_MODEL` in `.env`. The prompts are
model-agnostic — any model that can return clean JSON in `format=json`
mode will work.

## Loading and running phi4:14b in your existing Ollama container

Use this when the Ollama image is already pulled and a container
already exists on this host. You only need to load the model into it
and verify it serves on port `11434`.

### 1. Prerequisites

- Docker Desktop is running (whale icon in the tray says *Running*).
- An Ollama container already exists. The examples below assume the
  container is named `autocoder-phi4`. If yours has a different name,
  substitute it everywhere.
- ~10 GB free disk for the model layers.
- Port `11434` is free on the host (no native Ollama service running
  on Windows). Quick check from PowerShell:

  ```powershell
  Get-NetTCPConnection -LocalPort 11434 -ErrorAction SilentlyContinue
  ```

  If a non-Docker process holds it, stop it (`Stop-Process -Name
  ollama -Force`) before starting the container.

### 2. Confirm the container is running

```bash
docker ps --filter name=autocoder-phi4
```

Expected: one row with `STATUS = Up ...` and `PORTS = 0.0.0.0:11434->11434/tcp`.

If you see no rows but the container exists (it is just stopped):

```bash
docker start autocoder-phi4
docker ps --filter name=autocoder-phi4
```

### 3. Open a shell inside the container

For one-off commands you can use `docker exec` directly. For an
interactive session:

```bash
docker exec -it autocoder-phi4 bash
```

(`-it` allocates a TTY; without it you cannot use interactive prompts.
For non-interactive scripts, drop the `-i` and `-t`.)

### 4. Pull `phi4:14b` inside the container

From the container shell:

```bash
ollama pull phi4:14b
```

Or, in one shot from the host:

```bash
docker exec -it autocoder-phi4 ollama pull phi4:14b
```

This downloads ~9.1 GB. Expect 5–20 minutes depending on the network.
Progress bars print per layer; the final line should be `success`.

If the pull dies with
`tls: failed to verify certificate: x509: certificate signed by unknown authority`,
your network has a TLS-inspecting proxy. Inject the host's root CAs
into the container's trust store
(`/usr/local/share/ca-certificates/`), run `update-ca-certificates`,
then `docker restart autocoder-phi4` and retry the pull.

### 5. Verify the model is present and is the right variant

```bash
docker exec -it autocoder-phi4 ollama list
docker exec -it autocoder-phi4 ollama show phi4:14b
```

Expected:

- `ollama list` shows `phi4:14b` with `SIZE ≈ 9.1 GB`.
- `ollama show` prints `parameters 14.7B` and `quantization Q4_K_M`.

If either is off, you got a different variant — re-run step 4 with the
exact tag `phi4:14b`.

### 6. Smoke-test the model from the host

The container exposes `11434` on `localhost`. From the host (not
inside the container):

```bash
curl http://localhost:11434/api/tags
curl http://localhost:11434/api/chat \
  -d '{"model":"phi4:14b","messages":[{"role":"user","content":"say pong"}],"stream":false}'
```

Expected:

- `/api/tags` returns a JSON object listing `phi4:14b`.
- `/api/chat` returns a JSON object with a non-empty
  `message.content`. The first call can take 30–60 s while the model
  loads into RAM. Subsequent calls respond in seconds and stay warm
  for `OLLAMA_KEEP_ALIVE` minutes.

PowerShell users: `curl` is aliased to `Invoke-WebRequest` — use
`curl.exe` (or `Invoke-RestMethod`) and escape the inner JSON quotes:

```powershell
curl.exe http://localhost:11434/api/tags
curl.exe http://localhost:11434/api/chat -d "{\"model\":\"phi4:14b\",\"messages\":[{\"role\":\"user\",\"content\":\"say pong\"}],\"stream\":false}"
```

### 7. Wire the orchestrator to the container

`.env` only needs the endpoint and model name. Defaults already match
the container above:

```env
OLLAMA_ENDPOINT=http://localhost:11434
OLLAMA_MODEL=phi4:14b
OLLAMA_NUM_CTX=12288
OLLAMA_TEMPERATURE=0.2
OLLAMA_NUM_PREDICT=2048           # critical — 512 truncates feature plans mid-JSON
OLLAMA_TIMEOUT_SECONDS=600
```

Required port + runtime notes:

- Bind the container to **loopback only** so nothing on the LAN can
  reach it: `-p 127.0.0.1:11434:11434`. If your container was
  started with `-p 11434:11434` (binds 0.0.0.0), recreate it:

  ```bash
  docker rm -f autocoder-phi4
  docker run -d --name autocoder-phi4 --restart unless-stopped \
    -p 127.0.0.1:11434:11434 \
    -v autocoder-ollama-models:/root/.ollama \
    -e OLLAMA_NUM_THREAD=8 -e OLLAMA_KEEP_ALIVE=30m \
    ollama/ollama:latest
  ```

  The named volume `autocoder-ollama-models` preserves the downloaded
  model across container removals. Without it, step 4 has to re-run.
- `OLLAMA_NUM_THREAD=8` saturates an 8-thread CPU. Match your core
  count.
- `OLLAMA_KEEP_ALIVE=30m` keeps the model resident in RAM between
  requests. Lower it if you are memory-constrained.

Confirm the orchestrator can reach the model:

```bash
autocoder status                # exits cleanly if env is parseable
autocoder generate https://example.com   # first real call
```

The `autocoder` CLI calls
`OllamaClient.is_available()` before stage 4 and exits with a clear
message if the endpoint is unreachable.

### 8. Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `Cannot connect to the Docker daemon` | Docker Desktop is not running | Start Docker Desktop; wait for *Running* status. |
| `port is already allocated` on `docker run` | Native Ollama service holds 11434 | `Stop-Process -Name ollama -Force` (Windows) or `pkill ollama` (Linux/macOS), then retry. |
| `ollama pull` fails with `x509: certificate signed by unknown authority` | Corporate TLS inspector | Copy host root CAs into `/usr/local/share/ca-certificates/` in the container, run `update-ca-certificates`, restart. |
| `pull` restarts after container rebuild | Volume not mounted | Recreate the container with `-v autocoder-ollama-models:/root/.ollama`. |
| Orchestrator logs `ollama_unreachable` and exits | Container stopped or wrong endpoint | `docker start autocoder-phi4` and verify `OLLAMA_ENDPOINT` in `.env`. |
| First request takes 30–60 s, later ones are fast | Cold model load into RAM | Normal. `OLLAMA_KEEP_ALIVE=30m` keeps it warm. |
| `/api/chat` returns 404 or empty content | Wrong model name in request | Use the exact tag from `ollama list` (`phi4:14b`, not `phi-4` or `phi4`). |
| Orchestrator returns `OllamaError: Could not parse JSON ...` | Model returned prose despite `format=json` | Lower `OLLAMA_TEMPERATURE` (e.g. 0.1), or confirm the model is `phi4:14b` — smaller variants drift off-format more often. |
| Container memory pressure / OOM kills | WSL2 memory cap too low | Raise `memory` in `%USERPROFILE%\.wslconfig` to 16 GB+ and restart Docker Desktop. |
| `docker exec` says `OCI runtime exec failed` | Container is paused or unhealthy | `docker unpause autocoder-phi4` or `docker restart autocoder-phi4`. |

Lifecycle reference:

```bash
docker start  autocoder-phi4        # bring it back after reboot
docker stop   autocoder-phi4        # release CPU/RAM when done
docker logs -f autocoder-phi4       # tail inference logs
docker stats   autocoder-phi4       # watch CPU / RAM live
docker rm -f  autocoder-phi4        # drop the container (volume kept)
docker volume rm autocoder-ollama-models   # nuke the cached model (~9 GB)
```

## Client

`autocoder/llm/ollama_client.py` is the only place that talks to
Ollama. It is intentionally thin:

- One method, `chat_json(...)`, sets `format="json"`,
  `temperature=0.2`, `top_p=0.9`, and a configurable `num_predict`.
- `httpx` is used with a long read timeout (default 600 s) because
  CPU inference is slow.
- Streaming is off — the orchestrator wants one decoded JSON object,
  not a token stream.
- Logs are emitted with input/output token counts and wall time.

## Prompts

There are four prompt families across the codebase. All return a
single JSON object; all are short and contain no few-shot examples.

* `autocoder/llm/prompts.py:POM_SYSTEM` — POM-plan prompt (stage 4).
* `autocoder/llm/prompts.py:FEATURE_SYSTEM` — feature-plan prompt
  (stage 6).
* `autocoder/heal/prompts.py:HEAL_SYSTEM` — fill a single
  `NotImplementedError` stub (`autocoder heal`).
* `autocoder/heal/prompts.py:FAILURE_HEAL_SYSTEM` — revise a step
  body whose pytest run failed (`autocoder heal --from-pytest`).
  The envelope carries the step text + current body + Playwright
  error + a heuristic `failure_class` so the model can reason
  about disabled buttons, modal interception, wrong-kind widgets,
  and missing prerequisites.

Both heal prompts share the same validator
(`autocoder/heal/validator.py`); the failure-heal path opts in to
`max_statements=5` so a fix like
`pom.locate('agreement').check(); pom.click_submit()` is allowed.

### POM plan (stage 4)

System prompt (~120 tokens) sets the output schema and rules:

```
You are a planner for a Playwright test generator.
Output a single JSON object — no prose, no markdown.

Schema:
{
  "class_name": "<CamelCase>Page",
  "fixture_name": "<snake_case_page>",
  "methods": [
    {"name": "<snake_case>", "intent": "<<= 60 chars>",
     "element_id": "<id from elements>",
     "action": "click|fill|check|select|navigate|wait|expect_visible|expect_text",
     "args": ["<arg names if action needs values>"]}
  ]
}

Rules:
- Use ONLY element ids that appear in the input list.
- Choose `action` based on element kind: ...
- 1 method per element you intend to expose. Skip purely decorative ones.
- Keep total methods <= 20.
```

User prompt (~250-400 tokens) is a JSON envelope around the catalog:

```json
{
  "url": "https://app.example.com/login",
  "title": "Sign in",
  "class_name": "LoginPage",
  "fixture_name": "login_page",
  "elements": [
    {"id": "email", "kind": "input", "name": "Email"},
    {"id": "password", "kind": "input", "name": "Password"},
    {"id": "submit", "kind": "button", "name": "Sign in"}
  ],
  "forms": [
    {"id": "form_1", "fields": ["email", "password"], "submit_id": "submit"}
  ]
}
```

Output is typically ~120 tokens.

### Feature plan (stage 6)

System prompt (~150 tokens) defines the schema for Gherkin features
and constrains scenario tiers + step counts.

User prompt (~250-350 tokens) is again a JSON envelope:

```json
{
  "url": "https://app.example.com/login",
  "title": "Sign in",
  "headings": ["Sign in"],
  "pom_methods": ["fill_email", "fill_password", "click_submit"],
  "elements": [...],
  "tiers": ["smoke", "happy", "validation"]
}
```

Output is typically ~180 tokens.

## Validation

`autocoder/llm/validator.py` runs *before* any rendering. It:

- Drops methods whose `element_id` is not in the catalog.
- Drops duplicate method names.
- Rejects unknown action verbs.
- Auto-fills `args=["value"]` for `fill` / `select` if missing.
- Drops scenario steps whose `pom_method` is not in the POM plan
  (the step still survives, but its body becomes
  `raise NotImplementedError(...)` so a human can see exactly which
  assertions still need authoring).
- Dedupes scenarios by title.

Anything dropped is logged at WARN with the reason. No silent fixes.

## Plan cache

`autocoder/llm/plans.py` writes every validated plan to disk:

```
manifest/plans/<fixture>.pom.<fingerprint>.json
manifest/plans/<fixture>.feature.<tier_set>.<fingerprint>.json
```

On the next run, if the extraction fingerprint matches, the plan is
read from disk and the LLM is **not called**. `--force` ignores the
cache.

## Failure modes and recovery

| Failure                               | Detection                                  | Recovery |
|---------------------------------------|--------------------------------------------|----------|
| Model returns invalid JSON            | `json.loads` raises in `chat_json`         | Client retries the slice between the first `{` and last `}`. |
| Model invents an element id           | Validator drops the method                 | The remaining methods still render. |
| Model invents a POM method            | Validator nulls out `pom_method` on step   | The step renders as `NotImplementedError` so it cannot silently pass. |
| Ollama unreachable                    | `is_available()` check fails before stage 4| Orchestrator exits with the endpoint and a fix hint. |
| Generation timeout (CPU stalled)      | `httpx` read timeout (default 600 s)       | Run is reported failed; rerun resumes from the same URL. |
