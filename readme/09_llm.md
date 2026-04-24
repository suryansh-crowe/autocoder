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

## Quick start with the bundled compose file (recommended)

The repo ships `docker/ollama/Dockerfile` + `docker/ollama/docker-compose.yml`
that build an image with `phi4:14b` already loaded. One command
from the project root:

```bash
docker compose -f docker/ollama/docker-compose.yml up -d --build
```

That builds `autocoder-phi4:latest` (pulling `phi4:14b` into the
image during the build), starts the container bound to
`127.0.0.1:11434`, and applies `OLLAMA_KEEP_ALIVE=30m` +
`OLLAMA_NUM_THREAD=8`. See `docker/ollama/README.md` for options:
switching models (`--build-arg OLLAMA_MODEL=llama3.1:8b`),
lazy-pull at runtime (smaller image), GPU offload on Linux, and
Apple-Silicon native fallback.

If you don't want the compose path and need to load the model
into a pre-existing plain Ollama container by hand, the next
sections walk through that flow step by step.

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
| `port is already allocated` / `bind: Only one usage of each socket address` | Native Ollama service holds 11434 (Windows desktop app auto-starts on login) | `Get-Process ollama* \| Stop-Process -Force` (Windows) or `pkill ollama` (Linux/macOS). Disable **Ollama** in *Task Manager → Startup apps* to prevent recurrence, or change the container binding to `127.0.0.1:11435:11434` and set `OLLAMA_ENDPOINT=http://localhost:11435` in `.env`. |
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

## MCP-routed LLM flow

Autocoder no longer chooses between Ollama and Azure OpenAI itself.
`autocoder/llm/factory.py:get_llm_client(settings)` always returns the
MCP-backed client, and the sibling MCP server decides which concrete
provider should handle each request.

Autocoder still keeps the provider configuration in its own `.env`
because the MCP server loads that file through
`autocoder.config.load_settings(project_root=MCP_AUTOCODER_ROOT)`.

`.env` keys for the Azure provider:

```env
OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com
OPENAI_API_KEY=<your-data-plane-key>            # or AZURE_OPENAI_API_KEY
OPENAI_API_VERSION=2024-12-01-preview
AZURE_CHAT_DEPLOYMENT=gpt-4.1
AZURE_CHAT_TEMPERATURE=0.2
AZURE_CHAT_TOP_P=0.9
AZURE_CHAT_MAX_TOKENS=2048
AZURE_CHAT_TIMEOUT_SECONDS=120
```

`.env` keys for the local Ollama provider:

```env
OLLAMA_ENDPOINT=http://localhost:11434
OLLAMA_MODEL=phi4:14b
OLLAMA_TIMEOUT_SECONDS=1800
OLLAMA_NUM_CTX=4096
OLLAMA_TEMPERATURE=0.2
OLLAMA_TOP_P=0.9
OLLAMA_NUM_PREDICT=2048
```

Client-side MCP connection settings in autocoder:

```env
AUTOCODER_MCP_TRANSPORT=streamable-http
AUTOCODER_MCP_HTTP_HOST=127.0.0.1
AUTOCODER_MCP_HTTP_PORT=8000
AUTOCODER_MCP_HTTP_PATH=/mcp
```

The orchestrator logs `llm_backend_selected backend=mcp endpoint=...`
once at startup so runs stay auditable. Provider secrets are still
never logged; only presence is exposed through the existing secret-safe
settings helpers.

When autocoder needs an LLM response, it does **not** call Ollama or
Azure OpenAI directly. Instead it calls the sibling MCP server, and the
MCP server scores the available backends from request metadata such as
workload type, JSON strictness, prompt size, token budget, privacy
preference, and cost preference.

```text
autocoder planner / heal flow
  -> get_llm_client(settings)
  -> MCPClient
  -> MCP tool: llm_ping        (preflight)
  -> MCP tool: llm_chat        (actual request)
  -> MCP server metadata-based scoring
  -> OllamaClient or AzureOpenAIClient
  -> model response
  -> MCP response back to autocoder
  -> autocoder JSON recovery / validation / cache
```

Actual call chain:

1. The orchestrator or heal runner asks `factory.py` for the active
   client.
2. The factory returns `autocoder/llm/mcp_client.py:MCPClient`.
3. Startup preflight calls the MCP `llm_ping` tool instead of
   checking `/api/tags` or Azure directly.
4. Real planner / heal calls go through MCP `llm_chat` with:
   `system`, `user`, `purpose`, `json_mode`, `max_tokens`, and routing
   metadata inferred from the request.
5. The MCP server tool (`../mcp/src/mcp_server/tools/llm.py`) inspects
   that metadata and ranks the configured backends:
   - planner work with strict JSON and bigger prompts tends to score
     Azure higher
   - short/private/low-cost heal work tends to score Ollama higher
   - if the first pick fails and `MCP_LLM_ENABLE_FAILOVER=true`, the
     server retries once on the next-ranked backend
6. The MCP server loads autocoder's provider config from the sibling
   checkout, instantiates the chosen direct client, and performs the
   real model call there.
7. The MCP server returns structured metadata:
   selected backend, selected model, text, token counts, duration, and
   whether failover was used.
8. Back in autocoder, `MCPClient` logs the `llm_call` event and the
   existing JSON retry / parse / validate flow continues unchanged.

Simple selection examples:

1. A planning request such as `pom_plan:catalog_page` goes to MCP with
   metadata like:
   - workload = `pom_plan`
   - strict_json = `true`
   - prompt_chars = `9500`
   - prefer_speed = `4`
   - prefer_intelligence = `5`
   - prefer_json_reliability = `5`
2. The MCP server scores both configured backends.
   - Azure usually wins here because planning is a larger prompt and
     strict JSON matters a lot.
   - Ollama usually scores lower because privacy/cost are not the main
     priority for this request.
3. A heal request such as `heal:catalog_page:_i_click_submit` goes to
   MCP with metadata like:
   - workload = `heal`
   - strict_json = `true`
   - prompt_chars = `1800`
   - prefer_privacy = `4`
   - prefer_low_cost = `4`
4. Ollama often wins that request because the task is shorter and the
   privacy/cost signals are stronger.
5. If the chosen backend fails, the MCP server can retry once on the
   next-ranked backend when `MCP_LLM_ENABLE_FAILOVER=true`.

Tiny mental model:

- big planning task -> Azure/OpenAI is usually ranked higher
- small private/local heal task -> Ollama is usually ranked higher
- chosen backend fails -> MCP tries the second-best option

This is **tool-based MCP routing**, not MCP sampling. The backend
decision stays on your MCP server, which is why the model/provider
credentials remain server-side instead of moving back into the client.

## Client

`autocoder/llm/ollama_client.py` is the only place that talks to
Ollama. It is intentionally thin:

- One method, `chat_json(...)`, sets `format="json"`,
  `temperature=0.2`, `top_p=0.9`, and a configurable `num_predict`.
- **Streaming is on** — the client passes `stream=True` and
  accumulates newline-delimited JSON chunks. This keeps the HTTP
  connection active while Phi-4 generates, so the idle-read
  timeout only ever fires if Ollama genuinely stalls.
- **Per-purpose tuning.** Purposes starting with `heal:` /
  `heal_fail:` are capped at `num_predict=200` (heal bodies are
  1-5 short statements — a larger budget just lets the model
  ramble past the JSON-valid stop point) and run at
  `temperature=0.0` (greedy, the single highest-probability safe
  body; heal is not a creative task).
- `httpx` read timeout defaults to 1800 s (30 min) — that's the
  idle-between-chunks timer, so in streaming mode it effectively
  only matters during the silent **prompt-evaluation** phase
  before the first token arrives. Prompt eval on CPU with a 4 K
  context rarely exceeds 10 min; 30 min is comfortable headroom.
- `OLLAMA_NUM_CTX=4096` is the recommended default — our largest
  prompt is the feature plan at ~2.5 K tokens total, and a
  smaller context buffer cuts prompt-eval wall time ~3× on CPU
  compared to the older 12288.
- Logs are emitted with input/output token counts, wall time, and
  a per-10-second heartbeat (`ollama_stream_progress`
  `phase=prompt_eval|gen`) so a slow CPU run never looks hung.
  A dedicated `ollama_first_chunk prompt_eval_s=<float>` event
  fires the moment the first token arrives, so you can measure
  how much of each call was prompt-eval vs generation.

### JSON recovery ladder

Phi-4 at Q4_K_M occasionally returns JSON with one of: markdown
fences, a preamble sentence, an unterminated string near
`num_predict`, or a missing closing brace. `_try_parse_json` walks
a cheap-to-expensive ladder before giving up:

1. `json.loads(text.strip())`.
2. Strip markdown fences (` ```json … ``` `) and retry.
3. Slice the outermost balanced `{ … }` — scanning character by
   character and respecting quoted strings + escapes — then retry.
4. If the payload has an odd number of unescaped quotes or missing
   closing braces, append `"` / `}` / `]` as needed and retry.

If every step fails, `chat_json` fires **one retry** with the system
prompt extended by:

```
STRICT OUTPUT REQUIREMENTS:
- Respond with exactly ONE JSON object and nothing else.
- Do not wrap the response in markdown or prose.
- Close every string and every brace.
- Keep total output short enough to complete.
```

Only after the retry also fails (or the recovery ladder fails on the
retry response) does the client raise `OllamaError`. Events:
`ollama_json_retry attempt=0`, `ollama_json_recovered attempt=1` on
success, `ollama_json_parse_failed` on total failure.

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

### Heal prompt consequence rules

Both stub and failure heal prompts carry two new context fields
that constrain the LLM's output:

* **`forbidden_element_ids`** — element ids that prior When/And
  steps in the same scenario already clicked or filled. Computed
  by parsing the slug's newest `tests/generated/<run>/<slug>/<slug>.feature` file
  (`_scenario_prior_step_texts`) and fuzzy-matching each prior
  step text to a POM method's `element_id` or the extraction
  catalog (`_compute_forbidden_ids` in `heal/runner.py`). The LLM
  MUST NOT emit an assertion against any of those ids —
  re-asserting the action target is not a meaningful consequence
  test. The validator enforces this; rejected bodies fall back to
  `pass  # no safe binding`.
* **`page_url`** — the URL the extraction was done from. The
  system prompt explicitly forbids
  `expect(<fixture>.page).to_have_url(<page_url>)` because it is
  either trivially true (before navigation) or wrong (after
  navigation). When the step asserts arrival on a different page
  and no target URL is known, the LLM must emit
  `{"body": "pass", "intent": "no target url known"}`. The
  validator rejects the trivial form too.

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

System prompt (~400 tokens — the big one) defines the schema for
Gherkin features and now carries **component-aware coverage rules**.
Each interactive surface type the page exposes must produce a
dedicated scenario, and each absent type must not. Heuristics:

| Inventory field | Scenario the plan MUST include |
|-----------------|--------------------------------|
| `search` > 0    | Typed query → results indicator (rows / pagination / list); plus an empty-query validation scenario |
| `chat` > 0      | Prompt text → response / message area visible |
| `forms` > 0     | Valid submission + invalid / empty submission (validation) |
| `nav` > 0       | Click a nav link/tab → URL change or landmark heading for that target |
| `buttons` > 0   | Action button click → *consequence* assertion (dialog, toast, new panel) |
| `choices` > 0   | Toggle a checkbox/radio → dependent element becomes enabled/visible |
| `data` > 0      | Open a row OR sort/filter the table → detail or updated list visible |

The prompt also enforces a **Then-step quality** rule: every Then
step must describe a *consequence*, not the action target. "User
clicks Open assistant → Then the Open assistant button is visible"
is explicitly banned; the Then subject must name a different
element id (the chat panel, an Ask Stewie textbox, a new heading).

User prompt (~250-400 tokens) is a JSON envelope:

```json
{
  "url": "https://app.example.com/catalog",
  "title": "Catalog",
  "headings": ["Catalog"],
  "pom_methods": ["click_filter", "fill_search_assets", "click_home"],
  "elements": [...],
  "tiers": ["smoke", "happy", "validation"],
  "ui_inventory": {
    "search":  ["search_assets"],
    "chat":    [],
    "nav":     ["home", "close_sidebar"],
    "data":    [],
    "pagination": ["previous", "_1", "next"],
    "buttons": ["ask_stewie", "data_catalog", "source_connection", ...],
    "choices": [],
    "submits": [],
    "forms":   0,
    "headings": ["Catalog"]
  }
}
```

The inventory is computed by
`autocoder/llm/prompts.py:build_ui_inventory(extraction)` from the
extraction alone — no extra LLM cost. Output is typically
~350-700 tokens (up from 180) because the model now emits 3-8
scenarios instead of 2-6 when the page warrants it.

## Validation

`autocoder/llm/validator.py` runs *before* any rendering. It:

- Drops methods whose `element_id` is not in the catalog.
- Drops duplicate method names.
- Rejects unknown action verbs.
- Auto-fills `args=["value"]` for `fill` / `select` if missing.
- For scenario steps whose `pom_method` is not in the POM plan,
  tries a **close-match rebind** via `difflib.get_close_matches`
  (cutoff `0.75`). If a single close name exists, the step binds to
  that method and the validator logs
  `rebinding pom_method 'check_terms_of_service' -> 'check_terms_of_service_checkbox' (close match)`.
  If no close match exists, the binding is nulled and the validator
  logs the three nearest valid names so you can see what the model
  almost said.
- Dedupes scenarios by title.

A nulled `pom_method` still lets the renderer attempt synthesis
(navigation / assertion / negation patterns — see `10_generation.md`)
before falling back to `NotImplementedError`.

Anything dropped is logged at WARN with the reason. No silent fixes.

## Plan-level fallback

If `chat_json` ultimately raises `OllamaError` for the *feature plan*
call, `generate_feature_plan` does **not** abort the URL. It logs
`feature_plan_fallback` and returns a minimal `FeaturePlan` that
still references the POM class, so:

- The POM file still renders (the user does not lose a working POM to
  a bad LLM response).
- `tests/generated/<run>/<slug>/<slug>.feature` renders with one placeholder smoke
  scenario and a clear fallback description.
- The URL ends up as `needs_implementation` via the quality gate, not
  `failed`, so `autocoder generate --force` / `autocoder heal` can
  pick up the work once the LLM is healthy.

The POM plan does not have the same fallback — a broken POM plan
genuinely fails the URL, because everything downstream depends on
knowing which methods exist.

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
| Model returns invalid JSON            | `_try_parse_json` walks the recovery ladder | Fence strip → balanced-brace slice → unterminated-string repair. Failing that, one `chat_json` retry with a stricter system prompt. Only then `OllamaError`. |
| `OllamaError` on feature plan         | Raised after retry                          | `generate_feature_plan` logs `feature_plan_fallback` and returns a minimal `FeaturePlan` so the POM + steps still render; the URL ends up `needs_implementation`. |
| `OllamaError` on POM plan             | Raised after retry                          | The URL is marked `failed`; nothing downstream depends on a broken POM plan. |
| Model invents an element id           | Validator drops the method                  | The remaining methods still render. |
| Model invents a POM method            | Validator close-match rebinds via difflib   | If no close match, the binding is nulled and the renderer tries synthesis; ultimate fallback is `NotImplementedError`. |
| Ollama unreachable                    | `is_available()` check fails before stage 4 | Orchestrator exits with the endpoint and a fix hint. |
| Generation timeout (CPU stalled)      | `httpx` read timeout (default 600 s)        | Run is reported failed; rerun resumes from the same URL. |
