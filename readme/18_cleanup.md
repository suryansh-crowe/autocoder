# 18 · Cleanup — start from zero

Use this when you want a fully clean slate: no generated POMs, no
features, no step files, no manifest caches, no auth session. The next
`autocoder run` will then classify every URL, prompt for SSO again, and
regenerate from scratch.

## What to delete

| Path | Why |
|------|-----|
| `tests/features/*.feature` | Generated Gherkin features |
| `tests/steps/test_*.py` | Generated pytest-bdd step modules |
| `tests/pages/*_page.py` (except `base_page.py`) | Generated Page Object Models |
| `manifest/extractions/` | Cached page extractions |
| `manifest/plans/` | Cached POM + feature LLM plans |
| `manifest/heals/` | Cached stub-heal and failure-heal LLM suggestions |
| `manifest/runs/` | JUnit XML from previous verification passes |
| `manifest/registry.yaml` | The URL registry — wipe to forget every URL |
| `manifest/report.html` | Last HTML report from `autocoder report --html` |
| `.auth/user.json` | Playwright `storage_state` (cookies, localStorage) |
| `.auth/user.session_storage.json` | MSAL / SPA sessionStorage snapshot |
| `tests/__pycache__/`, `tests/pages/__pycache__/`, `tests/steps/__pycache__/` | Stale bytecode that can confuse pytest after regen |

## What to preserve

Do **not** delete these — they are hand-written framework/scaffolding:

- `tests/pages/base_page.py`
- `tests/__init__.py`, `tests/pages/__init__.py`, `tests/steps/__init__.py`
- `tests/conftest.py`
- `tests/support/`
- `tests/unit/`
- `tests/auth_setup/`
- `manifest/logs/` — historical run logs. Useful for post-mortems.
- `.env`, `urls.txt`, `pytest.ini` — user configuration.

## One-shot commands

### Bash (git-bash on Windows, or any Unix shell)

```bash
rm -rvf \
  tests/features/*.feature \
  tests/steps/test_*.py \
  tests/pages/agent_page.py \
  tests/pages/catalog_page.py \
  tests/pages/dq_insights_page.py \
  tests/pages/home_page.py \
  tests/pages/security_page.py \
  tests/pages/source_connection_page.py \
  tests/pages/sources_page.py \
  tests/pages/stewie_page.py \
  manifest/extractions \
  manifest/plans \
  manifest/heals \
  manifest/runs \
  manifest/registry.yaml \
  manifest/report.html \
  .auth/user.json \
  .auth/user.session_storage.json \
  tests/__pycache__ \
  tests/pages/__pycache__ \
  tests/steps/__pycache__
```

Enumerating each `_page.py` file by name is intentional — a blind
`tests/pages/*_page.py` glob would also match `base_page.py`, which you
want to keep. If you add new URLs, add the corresponding POM filenames
to the command.

### PowerShell

```powershell
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue `
  tests/features/*.feature, `
  tests/steps/test_*.py, `
  tests/pages/agent_page.py, `
  tests/pages/catalog_page.py, `
  tests/pages/dq_insights_page.py, `
  tests/pages/home_page.py, `
  tests/pages/security_page.py, `
  tests/pages/source_connection_page.py, `
  tests/pages/sources_page.py, `
  tests/pages/stewie_page.py, `
  manifest/extractions, `
  manifest/plans, `
  manifest/heals, `
  manifest/runs, `
  manifest/registry.yaml, `
  manifest/report.html, `
  .auth/user.json, `
  .auth/user.session_storage.json, `
  tests/__pycache__, `
  tests/pages/__pycache__, `
  tests/steps/__pycache__
```

`-ErrorAction SilentlyContinue` means missing files don't abort the
command — safe to run on a partially-clean tree.

## Verify the slate is clean

```bash
ls tests/features tests/pages tests/steps manifest/ .auth/
```

Expected:

```
tests/features:

tests/pages:
__init__.py
base_page.py

tests/steps:
__init__.py

manifest/:
logs

.auth/:
```

## Regenerate from zero

```bash
autocoder run --urls-file urls.txt
autocoder report --run --html manifest/report.html
```

The first command classifies every URL, performs SSO (browser pops
up because `.auth/user.json` is gone), extracts the authenticated
DOM, generates POMs / features / steps / auth-setup, and auto-heals
any unresolved step bodies. The second runs pytest, parses the JUnit
XML, and writes a standalone HTML dashboard.

## Partial cleans

When you only want a subset, pick the lines above that apply. Common
cases:

- **Forget one URL** — delete its `tests/pages/<slug>_page.py`,
  `tests/features/<slug>.feature`, `tests/steps/test_<slug>.py`,
  `manifest/extractions/<slug>.json`, `manifest/extractions/<slug>.prev.json`,
  `manifest/runs/<slug>.xml`, and the `nodes: <url>:` block in
  `manifest/registry.yaml`.
- **Force LLM to re-plan without re-auth** — delete `manifest/plans/`
  and `manifest/heals/`. Keep `.auth/` so you don't have to sign in
  again. Then re-run with `--force`.
- **Keep generation but re-run tests** — delete `manifest/runs/` only,
  then `autocoder report --run`.

## Auth-only reset (`autocoder auth-reset`)

Don't want to wipe generated tests / extractions / plans — just want
to force re-authentication on the next run? Use the dedicated CLI:

```bash
autocoder auth-reset            # interactive confirm
autocoder auth-reset --yes      # scripts / CI — no prompt
autocoder auth-reset --keep-spec  # clear session files only; keep
                                  #   the registry AuthSpec (selectors,
                                  #   auth_kind). Use when the login
                                  #   page hasn't changed, just the
                                  #   session expired.
```

What it removes (all with absent-file tolerance):

- `.auth/user.json` — Playwright storage state (cookies + localStorage).
- `.auth/user.session_storage.json` — MSAL sessionStorage snapshot.
- `registry.auth` entry in `manifest/registry.yaml` (unless `--keep-spec`).

Nothing else is touched — your generated POMs, features, steps,
extraction snapshots, and plan caches all stay intact. The next
`autocoder run` will re-detect the login page, re-render
`tests/auth_setup/test_auth_setup.py`, and perform the in-process
login flow again. Captured sessions are reinstated in
`.auth/user.json` when the flow completes, and the settle step moves
the browser off the OAuth return URL before extraction proceeds.
