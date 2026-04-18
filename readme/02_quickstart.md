# 02 · Quickstart

End-to-end in five steps. Each step is verifiable on its own — do not
skip the verification.

## 1. Python environment

```bash
python -m venv .venv
. .venv/Scripts/activate          # Windows; use .venv/bin/activate elsewhere
pip install -r requirements.txt
playwright install chromium
pip install -e .                   # exposes the `autocoder` CLI entry point
```

**Verify:** `autocoder --help` prints the subcommands.

## 2. Phi-4 14B in Docker

The orchestrator only needs `http://localhost:11434` reachable. Quick
recipe (see `09_llm.md` for full Docker walkthrough):

```bash
docker volume create autocoder-ollama-models
docker run -d --name autocoder-phi4 --restart unless-stopped \
  -p 11434:11434 \
  -v autocoder-ollama-models:/root/.ollama \
  -e OLLAMA_NUM_THREAD=8 -e OLLAMA_KEEP_ALIVE=30m \
  ollama/ollama:latest
docker exec -it autocoder-phi4 ollama pull phi4:14b
```

**Verify:** `curl http://localhost:11434/api/tags` returns JSON listing
`phi4:14b`.

## 3. Configure `.env`

```bash
cp .env.example .env
```

Required for protected URLs:

```env
BASE_URL=https://app.example.com
LOGIN_URL=https://app.example.com/login   # optional; auto-detected when omitted
LOGIN_USERNAME=...                         # never commit
LOGIN_PASSWORD=...                         # never commit
```

Secrets stay in your local `.env`. They are never written into
generated code, the registry, or LLM prompts. See `06_auth_first.md`.

## 4. First generation

URLs can come from three places (priority: CLI args > `--urls-file` >
`AUTOCODER_URLS` in `.env`). Pick whichever fits your workflow:

```bash
# (a) Pass URLs as CLI args
autocoder generate https://app.example.com/login \
                   https://app.example.com/dashboard

# (b) Read from a file (one URL per line, '#' comments allowed)
echo "https://app.example.com/login"     >  urls.txt
echo "https://app.example.com/dashboard" >> urls.txt
autocoder generate --urls-file urls.txt

# (c) Read from .env
#     AUTOCODER_URLS=https://app.example.com/login,https://app.example.com/dashboard
autocoder generate
```

You should see:

- `tests/pages/login_page.py`, `tests/pages/dashboard_page.py`
- `tests/features/login.feature`, `tests/features/dashboard.feature`
- `tests/steps/test_login.py`, `tests/steps/test_dashboard.py`
- `tests/auth_setup/test_auth_setup.py`
- `manifest/registry.yaml` populated with each URL's status

**Verify:** `autocoder status` prints a table of every URL with status
`complete`.

## 5. Run the suite

When an authenticated URL is in scope, run the auth setup once to
capture a session, then run the rest of the suite normally:

```bash
pytest tests/auth_setup -m auth_setup     # one-time / when creds rotate
pytest -m smoke                            # smoke tier
pytest                                     # everything
```

`tests/.auth/user.json` is the cached session; subsequent test runs
reuse it via `storage_state`. See `12_running_tests.md`.

## Day-2 commands

| Goal | Command |
|------|---------|
| Add new URLs to the registry | `autocoder generate <new-urls>` |
| Reprocess everything in the registry | `autocoder rerun` |
| Force regeneration ignoring cache | `autocoder generate --force <urls>` |
| Add a tier to an existing URL | `autocoder extend --tier regression <url>` |
| Inspect status | `autocoder status` |

## Running the workflow end-to-end

This is the full operating loop once `.env` is configured. It covers
the orchestrator script (`autocoder generate`) — *not* just the test
runner. Use it the first time and every time you want to (re)generate
artifacts for the URLs in your registry.

### Step 1 — Prerequisites

Before invoking the script, all of these must be true:

- The Python venv is active (`. .venv/Scripts/activate`) and the
  `autocoder` CLI is installed (`autocoder --help` works).
- Playwright's Chromium is installed (`playwright install chromium`
  was run once).
- The Ollama container is up and `phi4:14b` is loaded
  (`docker ps --filter name=autocoder-phi4` shows `Up`, and
  `curl http://localhost:11434/api/tags` returns JSON listing
  `phi4:14b`). See `09_llm.md` for the Docker walkthrough.
- `.env` is filled in: `BASE_URL`, optionally `LOGIN_URL`, and any
  `LOGIN_USERNAME` / `LOGIN_PASSWORD` needed for protected URLs.
- The URLs you want to process are reachable from this machine.

### Step 2 — Confirm the environment is ready

A 30-second health check before you run anything:

```bash
autocoder --help                              # CLI is installed
docker ps --filter name=autocoder-phi4        # container is Up
curl http://localhost:11434/api/tags          # phi4:14b is loaded
autocoder status                              # registry is readable
```

`autocoder status` prints either the existing registry table or
"Registry is empty" — both are fine. An exception here means `.env`
is malformed or `manifest/` is unwritable.

### Step 3 — Run the orchestrator script

The single command that drives everything:

```bash
autocoder generate [URLS...] [--urls-file FILE] [--tier TIER] [--force] [--skip-llm]
```

URL source priority is **CLI args > `--urls-file` > `AUTOCODER_URLS`
env var**. The first non-empty source wins; the others are ignored.

Examples:

```bash
# (1) Single URL on the command line
autocoder generate https://app.example.com/dashboard

# (2) Multiple URLs (login + two protected pages)
autocoder generate https://app.example.com/login \
                   https://app.example.com/dashboard \
                   https://app.example.com/reports

# (3) Read URLs from a file (one per line; '#' comments and blanks ignored)
autocoder generate --urls-file urls.txt

# (4) Read URLs from .env (no CLI args, no --urls-file)
#     AUTOCODER_URLS=https://app.example.com/login,https://app.example.com/dashboard
autocoder generate

# (5) Pick which scenario tiers to generate (default: smoke + happy + validation)
autocoder generate --tier smoke --tier regression \
                   https://app.example.com/dashboard

# (6) Re-extract + replan everything ignoring the on-disk caches
autocoder generate --force https://app.example.com/dashboard

# (7) Browser + extraction only — no LLM calls (useful when Ollama is offline)
autocoder generate --skip-llm https://app.example.com/dashboard
```

Invalid URLs (missing scheme, missing host, non-http/https) cause an
immediate exit with the offending URL plus the source it came from
(`CLI args`, `--urls-file <path>`, or `$AUTOCODER_URLS`). If no source
yields any URL, the CLI exits with the three-option usage hint.

The console streams a status line per stage. A successful run ends
with a table of generated artifacts and exit code 0.

### Step 4 — What the script will do

For each URL you pass, the orchestrator runs an 8-stage pipeline:

1. **Classify** — open the URL in a real browser (anonymous), decide
   if it is public, login, or auth-protected.
2. **Auth-first** — if any URL needs auth, probe the login page,
   render `tests/auth_setup/test_auth_setup.py`, and persist the
   `AuthSpec` in the registry.
3. **Extract** — visit each URL (with `storage_state` if available),
   capture a compact element catalog, fingerprint it.
4. **POM plan** *(LLM call #1)* — Phi-4 returns a JSON plan listing
   the methods to expose on the POM. Validated against the catalog.
5. **POM render** — template emits `tests/pages/<slug>_page.py`.
6. **Feature plan** *(LLM call #2)* — Phi-4 returns a JSON plan
   listing scenarios for the requested tiers.
7. **Feature + Steps render** — templates emit
   `tests/features/<slug>.feature` and `tests/steps/test_<slug>.py`.
8. **Persist** — registry status advances to `complete`; runs.log
   appends a JSON event line.

If a URL's fingerprint matches the previous run, stages 4-7 are
skipped entirely (zero LLM tokens). See `04_pipeline.md` for the
full diagram.

### Step 5 — Where the generated outputs live

Every output path is keyed by the URL's slug, so multiple URLs never
collide.

| Type | Path |
|------|------|
| POM (one per URL) | `tests/pages/<slug>_page.py` |
| Feature (one per URL) | `tests/features/<slug>.feature` |
| Step definitions (one per URL) | `tests/steps/test_<slug>.py` |
| Auth setup (one per project) | `tests/auth_setup/test_auth_setup.py` |
| Registry (single index) | `manifest/registry.yaml` |
| Per-URL extraction snapshot | `manifest/extractions/<slug>.json` |
| Cached LLM plans | `manifest/plans/<slug>_page.{pom,feature}.<fp>.json` |
| Run log | `manifest/runs.log` |

Confirm by running:

```bash
autocoder status         # table of every URL + its current status
ls tests/pages tests/features tests/steps
ls manifest/extractions manifest/plans
```

### Step 6 — Rerun or continue from where it stopped

The orchestrator persists progress **after every URL**, not at the
end. If a run is interrupted (Ctrl-C, crash, network blip), each URL's
`status` field in `manifest/registry.yaml` records exactly how far it
got: `pending → extracted → pom_ready → feature_ready → steps_ready →
complete`.

To resume:

```bash
autocoder rerun
```

This reprocesses every URL in the registry. For each one the
orchestrator decides automatically:

- Already `complete` and page unchanged → skip stages 4-7 (zero LLM
  tokens).
- Page changed since last run → fingerprint differs, plan cache
  misses, regenerate the affected artifacts.
- Mid-pipeline status (e.g. `pom_ready`) → continue from the next
  unfinished stage; reuse cached extraction + POM plan.

To force a clean rebuild:

```bash
autocoder generate --force <urls>
```

To add coverage tiers to existing URLs without duplicating scenarios:

```bash
autocoder extend --tier regression --tier edge <urls>
```

`autocoder status` is the resume map — it prints the table of URLs
and current status before you decide what to run.

### Step 7 — How this differs from "just running tests"

| Activity | Command | What it does |
|----------|---------|--------------|
| **Workflow / orchestrator** | `autocoder generate ...` | Drives a real browser, calls Phi-4 to plan, renders the test files, updates the manifest. Produces or updates the suite. |
| **Test execution** | `pytest ...` | Runs the suite produced by the orchestrator. Reads `tests/`, hits the app under test, reports pass/fail. |

You run the orchestrator when:

- You first set up the project on a new app.
- You add or change which URLs are in scope.
- The app's UI changes and your existing tests start to drift.
- You want to add a new scenario tier to an existing URL.

You run pytest when:

- You want to execute an already-generated suite.
- CI runs the suite on every commit.
- You are debugging a single feature/scenario.

The orchestrator never executes tests. pytest never edits the
generated files. The two are independent: you can re-run pytest as
often as you like with no side effects on the generated suite, and
re-running the orchestrator never touches `pytest`'s reports or
caches.

### Step 8 — Troubleshooting common run issues

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| `ollama_unreachable: endpoint=...` and the run exits | Container stopped or wrong endpoint in `.env` | `docker start autocoder-phi4`, verify with `curl http://localhost:11434/api/tags`. |
| `OllamaError: Could not parse JSON ...` | Model returned prose instead of JSON | Lower `OLLAMA_TEMPERATURE` to `0.1`; confirm `OLLAMA_MODEL=phi4:14b` (smaller variants drift). |
| `auth_form_not_detected` warning, auth setup not written | Login page uses a non-standard form (e.g. SSO redirect with no visible password field) | Set `LOGIN_URL` explicitly in `.env`; if the SSO page is on another origin, capture `storage_state` manually first. |
| `auth_needed_but_no_login_url` | Protected URLs in the input but no login URL was discovered | Add `LOGIN_URL=...` to `.env`, or include the login URL in the `autocoder generate` argument list. |
| Run exits before any URL finishes; `manifest/runs.log` shows `extract_failed` | URL unreachable or load timeout | Check the URL in a normal browser. Increase `EXTRACTION_NAV_TIMEOUT_MS` in `.env` if the app is slow to load. |
| Generated POM has fewer methods than expected | Element catalog hit `MAX_ELEMENTS_PER_PAGE` (default 60), or the LLM dropped methods that referenced unknown ids | Bump `MAX_ELEMENTS_PER_PAGE`; check `manifest/runs.log` for `pom_plan_issue` warnings. |
| Generated step body says `raise NotImplementedError("Implement step: ...")` | Validator could not bind the step to a POM method | Either add the missing method to the POM and re-run, or replace the body manually. The step never silently passes. |
| `autocoder rerun` re-runs LLM on every URL | Fingerprints changed (real UI drift) or `--force` was passed | Look at `manifest/runs.log` for `pom_plan_cache_hit` vs cache miss events. Drop `--force` if you set it. |
| Permission errors writing under `tests/` or `manifest/` | OneDrive / antivirus locking files | Pause sync on the project directory while the orchestrator runs, or move the project off OneDrive. |
| Long stalls between `ollama_call` log lines | First request loads the 9 GB model into RAM; later requests reuse it | Normal on a cold container. `OLLAMA_KEEP_ALIVE=30m` keeps the model warm. |
| `autocoder status` shows `failed` for a URL | Last run hit an unrecoverable error on that URL | Check `manifest/runs.log` for the matching `error` event; fix the cause and re-run — failed nodes are retried automatically. |
