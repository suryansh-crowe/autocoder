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
LOGIN_PASSWORD=                                 # optional for SSO; required for inline-form login
HEADLESS=false                                  # required for first SSO capture so you can finish MFA
OLLAMA_NUM_PREDICT=2048                         # critical: 512 truncates feature plans
AUTH_INTERACTIVE_TIMEOUT_MS=                    # optional ms override; default 300000 headed / 90000 headless
LOG_LEVEL=info                                  # debug | info | warn | error
```

`LOGIN_PASSWORD` is **only** required when the app uses a classic
inline username+password form. Microsoft/Google/GitHub SSO, magic
link, OTP, and email-only flows work with `LOGIN_USERNAME` alone —
the orchestrator fills the password on the IdP page if one is
configured AND the page asks for it, otherwise it waits for you to
complete the flow interactively in the visible browser window.

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

## 5. Generate, test, and heal in one shot

For most users the right command is `autocoder run`, which chains
generation -> pytest -> heal -> pytest until every slug passes or the
heal budget is spent:

```bash
# (a) CLI args
autocoder run https://app.example.com/login https://app.example.com/dashboard

# (b) Plain-text file (one URL per line, '#' comments allowed)
autocoder run --urls-file urls.txt

# (c) From .env (BASE_URL + LOGIN_URL configured above)
autocoder run
```

Common flags:

| Flag | Meaning |
|------|---------|
| `--tier smoke\|happy\|validation\|regression\|edge\|...` | Scenario tiers. Defaults to smoke + happy + validation. |
| `--force` | Ignore caches and rebuild every artifact. |
| `--skip-llm` | Run intake + extraction only; do not call the LLM. |
| `--max-heal-attempts N` | Upper bound on heal passes per failing test file. Defaults to **3**. Set `0` to run pytest once and skip healing. |

The command exits non-zero when any URL's tests still fail at the end
of the cycle, so CI pipelines reliably catch it. Final per-URL state
is one of:

| Final state | Meaning |
|-------------|---------|
| `verified` | generation + pytest all passing (possibly after healing). |
| `needs_implementation` | generation finished but some tests still fail after the heal budget. Run `autocoder heal --from-pytest --slug <slug>` to try again, or hand-edit. |
| `failed` | generation itself failed; see the `url_failed` log line. |

If you only want generation without running pytest (e.g. during
offline development, or when the real app is not reachable):

```bash
autocoder generate <urls>
```

Other flags are identical. Healing after the fact is available via
`autocoder heal --from-pytest`.

Output:

- `tests/pages/<slug>_page.py` — generated POMs
- `tests/features/<slug>.feature` — Gherkin
- `tests/steps/test_<slug>.py` — pytest-bdd step modules
- `tests/auth_setup/test_auth_setup.py` — rendered whenever auth is in scope; the renderer picks a template for the detected `auth_kind` (form / sso / username-first / email-only)
- `.auth/user.json` — Playwright storage state. For `form` and
  `sso_microsoft` flows, `autocoder generate` performs the login
  in-process and writes this file automatically. For flows that
  require an external step (magic link, OTP, MFA), the orchestrator
  persists whatever cookies it could collect and surfaces
  `auth_session_awaiting_external` — you run the rendered
  `test_auth_setup.py` headful once to finish it.
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

## 6b. (optional) Run pytest by itself

`autocoder run` already invokes pytest for you. This section is only
relevant when you want to re-run the suite manually — for example
after hand-editing a step file:

## 7. Run the suite

For most setups `autocoder generate` has already captured the
authenticated session, so you can skip straight to running the
generated tests:

```bash
pytest tests/steps/test_login.py --headed     # watch in a browser first
pytest -m smoke                                # smoke tier
pytest                                         # everything
```

`.auth/user.json` holds the captured session; every generated test
inherits it via the `browser_context_args` fixture in
`tests/conftest.py`.

If the run log ends with `auth_session_awaiting_external` (magic
link, OTP, or tenant MFA that the runner can't complete on its own),
run the rendered setup headful to finish the flow once:

```bash
HEADLESS=false pytest tests/auth_setup -m auth_setup
```

After that, subsequent `autocoder generate` / `pytest` runs just
reuse `.auth/user.json` until the session expires.

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
| Tail latest run log | `tail -f manifest/logs/$(ls -t manifest/logs \| head -1)` |

## When something fails

| Symptom | Fix |
|---------|-----|
| `ollama_unreachable` | `docker start autocoder-phi4`; `curl http://localhost:11434/api/tags` |
| `OllamaError: Could not parse JSON ... Unterminated string` | Raise `OLLAMA_NUM_PREDICT` in `.env` (2048+). The client already retries once with a stricter prompt and recovers fenced / truncated JSON before raising. |
| `auth_session_not_captured reason=missing_credentials` | Set `LOGIN_USERNAME` in `.env`. |
| `auth_session_not_captured reason=missing_password_for_password_mode` | Classic username+password app is detected and `LOGIN_PASSWORD` is not set. Set it — SSO modes do NOT trigger this any more. |
| `auth_sso_headless` warning | You are running an SSO flow with `HEADLESS=true`. Enterprise Entra tenants need MFA, which can't be completed headless. Set `HEADLESS=false` in `.env` and rerun. |
| `auth_session_not_captured reason=success_indicator_not_seen` | Runner timed out waiting for the page to leave `login.microsoftonline.com` / `/login`. Usually MFA was not completed. Rerun with `HEADLESS=false` and finish the prompt, or raise `AUTH_INTERACTIVE_TIMEOUT_MS`. |
| `url_skipped_awaiting_auth` | A protected URL was deliberately skipped because no authenticated session exists yet. Complete auth (see `auth_session_*` events) and rerun. |
| `auth_session_awaiting_external` | Magic-link / OTP flow. Complete the external step once via `HEADLESS=false pytest tests/auth_setup -m auth_setup`. The cookies the runner already collected are still saved, so the follow-up starts warm. |
| `auth_probe_navigated status=200` then no `auth_mode_detected` | Login page has no recognisable controls. Check the screenshot/HTML in `manifest/logs/nav_timeout_*` or `auth_failure_*`, then either widen the detection in `auth_probe.py` or override the tenant selectors via `AUTH_MSFT_*` env vars. |
| SSO login fails on custom Entra tenant | Set `AUTH_MSFT_EMAIL_SELECTOR` / `AUTH_MSFT_NEXT_SELECTOR` / `AUTH_MSFT_PASSWORD_SELECTOR` / `AUTH_MSFT_SUBMIT_SELECTOR` / `AUTH_MSFT_KMSI_SELECTOR` in `.env` to match the tenant's markup. |
| `run_done_with_issues needs_implementation=N` | Some step texts could not be bound or synthesized. Inspect the `steps_incomplete` log lines for the file paths, then `autocoder heal --slug <slug>` or hand-edit the placeholder bodies. |
| `NotImplementedError: Implement step: ...` | `autocoder heal --slug <slug>` |
| Playwright timeout / disabled / pointer-intercepted | `autocoder heal --from-pytest --slug <slug>` |
| Generated POM points at the wrong widget (e.g. `fill_email` on a checkbox) | `autocoder generate --force <url>` — the inspector distinguishes `<input type=checkbox>` from text inputs. |
| Click fails with `element is not enabled` | Should self-heal. If it doesn't, the control depending on the button isn't a checkbox; hand-edit the POM method or add the prerequisite to the scenario. `BasePage.click(id, heal=False)` opts out for negative tests. |
| Tests for `/stewie` (or any authenticated URL) describe the consent shell, not the real app | The authenticated DOM at that URL equals the anonymous DOM. Extract the real authenticated landing URL instead (e.g. `/dashboard` after sign-in) and add it to your input list. See `10_generation.md` → "Caveat for authenticated SPAs". |

Full docs: `readme/README.md` (15 numbered docs + `17_heal.md`).
