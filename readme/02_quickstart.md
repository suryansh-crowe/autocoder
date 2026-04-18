# 02 · Quickstart

Eight steps from clean checkout to a green test run. Each step is
idempotent and verifiable on its own.

## 1. Install

```bash
python -m venv .venv
. .venv/Scripts/activate         # Windows; .venv/bin/activate elsewhere
pip install -r requirements.txt
pip install -e .
playwright install chromium
```

**Verify:** `autocoder --help` lists `generate / extend / heal / rerun / status`.

## 2. Start Phi-4 in Docker (loopback-only)

```bash
docker volume create autocoder-ollama-models
docker run -d --name autocoder-phi4 --restart unless-stopped \
  -p 127.0.0.1:11434:11434 \
  -v autocoder-ollama-models:/root/.ollama \
  -e OLLAMA_NUM_THREAD=8 -e OLLAMA_KEEP_ALIVE=30m \
  ollama/ollama:latest
docker exec -it autocoder-phi4 ollama pull phi4:14b
```

**Verify:** `curl http://localhost:11434/api/tags` lists `phi4:14b`.
Full Docker walkthrough: `09_llm.md`.

## 3. Configure `.env`

```bash
cp .env.example .env
```

Minimum to set:

```env
BASE_URL=https://app.example.com
LOGIN_URL=https://app.example.com/login        # optional; auto-detected
LOGIN_USERNAME=                                 # never commit
LOGIN_PASSWORD=                                 # never commit
OLLAMA_NUM_PREDICT=2048                         # critical: 512 truncates feature plans
LOG_LEVEL=info                                  # debug | info | warn | error
```

URLs to process come from this priority chain:

```
CLI args  >  --urls-file  >  AUTOCODER_URLS env  >  [LOGIN_URL, BASE_URL] from .env
```

Secrets stay in `.env` only — never logged, never embedded in
generated files. See `06_auth_first.md` and `15_logging.md`.

## 4. Confirm everything is wired

```bash
python scripts/verify_local_llm.py             # PASS = inference is local-only
autocoder status                                # registry view (empty on first run)
```

## 5. Generate

Pick whichever URL source fits your workflow:

```bash
# (a) CLI args
autocoder generate https://app.example.com/login https://app.example.com/dashboard

# (b) Plain-text file (one URL per line, '#' comments allowed)
autocoder generate --urls-file urls.txt

# (c) From .env (BASE_URL + LOGIN_URL configured above)
autocoder generate
```

Other flags: `--tier smoke|happy|validation|regression|edge|...`,
`--force` (ignore caches), `--skip-llm` (browser + extraction only).

Output:

- `tests/pages/<slug>_page.py` — generated POMs
- `tests/features/<slug>.feature` — Gherkin
- `tests/steps/test_<slug>.py` — pytest-bdd step modules
- `tests/auth_setup/test_auth_setup.py` — only when an auth URL is in scope
- `manifest/registry.yaml` + `manifest/extractions/` + `manifest/plans/`

## 6. Heal what the renderer left as `NotImplementedError`

The renderer leaves an explicit `raise NotImplementedError(...)` for
any step it could not safely bind to a POM method. Fill them in via
the LLM:

```bash
autocoder heal --slug login --dry-run         # preview
autocoder heal --slug login                    # apply
```

Cached in `manifest/heals/`; reruns of unchanged stubs cost zero
tokens.

## 7. Run the suite

```bash
# auth-setup runs once (or whenever credentials rotate)
pytest tests/auth_setup -m auth_setup

# the rest
pytest tests/steps/test_login.py --headed     # watch in a browser first
pytest -m smoke                                # smoke tier
pytest                                         # everything
```

`tests/.auth/user.json` holds the captured session; subsequent runs
inherit it via `storage_state`.

## 8. Heal runtime failures (loop until green or until human input is needed)

```bash
autocoder heal --from-pytest --slug login
pytest tests/steps/test_login.py
```

`--from-pytest` runs pytest, captures every failure with its
Playwright error message, and asks the LLM for a revised step body
(up to 5 statements so prerequisites like "tick the agreement
checkbox first" are expressible). All suggestions are AST-validated
against the POM's real method list. See `17_heal.md`.

Stop when the only remaining failures are app-specific decisions:
the real success URL, real credentials, real MFA flow.

## Day-2 commands

| Goal | Command |
|------|---------|
| Add new URLs to the registry | `autocoder generate <new-urls>` |
| Reprocess everything | `autocoder rerun` |
| Force rebuild ignoring caches | `autocoder generate --force <urls>` |
| Add a coverage tier | `autocoder extend --tier regression <urls>` |
| Inspect status | `autocoder status` |
| Heal stubs | `autocoder heal [--slug X] [--dry-run]` |
| Heal runtime failures | `autocoder heal --from-pytest [--slug X]` |
| Verify local-only | `python scripts/verify_local_llm.py` |
| Tail run log | `tail -f manifest/runs.log` |

## When something fails

| Symptom | Fix |
|---------|-----|
| `ollama_unreachable` | `docker start autocoder-phi4`; `curl http://localhost:11434/api/tags` |
| `OllamaError: Could not parse JSON ... Unterminated string` | `OLLAMA_NUM_PREDICT=2048` in `.env` |
| `auth_form_not_detected` | App uses external SSO (no inline password). Capture `storage_state` by hand or set `LOGIN_URL` explicitly. |
| `NotImplementedError: Implement step: ...` | `autocoder heal --slug <slug>` |
| Playwright timeout / disabled / pointer-intercepted | `autocoder heal --from-pytest --slug <slug>` |
| Generated POM points at the wrong widget (e.g. `fill_email` on a checkbox) | `autocoder generate --force <url>` — the inspector now distinguishes `<input type=checkbox>` from text inputs. |

Full docs: `readme/README.md` (15 numbered docs + `17_heal.md`).
