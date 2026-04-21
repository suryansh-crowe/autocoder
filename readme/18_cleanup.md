# 18 · Cleanup — start from zero

Use this when you want a fully clean slate: no generated POMs, no
features, no step files, no manifest caches, no auth session. The next
`autocoder run` will then classify every URL, prompt for SSO again, and
regenerate from scratch.

## What to delete

| Path | Why |
|------|-----|
| `tests/generated/generated_*/` | Every per-run folder — generated tests, POMs, features, xml, and the run's own `manifest/` (registry, extractions, plans, heals, logs). Self-contained; no shared cache elsewhere. |
| `.auth/user.json` | Playwright `storage_state` (cookies, localStorage) |
| `.auth/user.session_storage.json` | MSAL / SPA sessionStorage snapshot |
| `tests/__pycache__/`, `tests/generated/**/__pycache__/` | Stale bytecode that can confuse pytest after regen |

There is no root-level `manifest/` directory to clean — every run
folder carries its own. If an older version of the tool re-created
one, it is still gitignored and safe to delete.

## What to preserve

Do **not** delete these — they are hand-written framework/scaffolding:

- `tests/pages/base_page.py` — shared Playwright helper POMs extend.
- `tests/generated/__init__.py`, `tests/generated/conftest.py` — latest-bundle filter.
- `tests/__init__.py`, `tests/pages/__init__.py`
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
  tests/generated/generated_* \
  .auth/user.json \
  .auth/user.session_storage.json \
  tests/__pycache__ \
  tests/generated/__pycache__
```

The bundle layout makes cleanup a one-line glob — every generated
artefact AND every manifest artefact is under
`tests/generated/generated_*/`, so there is no risk of matching the
hand-written `base_page.py`. Running a fresh `autocoder generate`
after this produces a brand-new run folder that starts cold (no
prior seed to copy from).

### PowerShell

```powershell
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue `
  tests/generated/generated_*, `
  .auth/user.json, `
  .auth/user.session_storage.json, `
  tests/__pycache__, `
  tests/generated/__pycache__
```

`-ErrorAction SilentlyContinue` means missing files don't abort the
command — safe to run on a partially-clean tree.

## Verify the slate is clean

```bash
ls tests/generated tests/pages .auth/
```

Expected:

```
tests/generated:
__init__.py
conftest.py

tests/pages:
__init__.py
base_page.py

.auth/:
```

No stray `manifest/` anywhere — the next `autocoder generate`
creates a fresh run folder under `tests/generated/` that contains
its own `manifest/`.

## Regenerate from zero

```bash
autocoder run --urls-file urls.txt
autocoder report --run --html manifest/report.html
```

The first command classifies every URL, performs SSO (browser pops
up because `.auth/user.json` is gone), extracts the authenticated
DOM, generates POMs / features / steps / auth-setup into a fresh
`tests/generated/generated_<timestamp>/` folder, and auto-heals any
unresolved step bodies. The second runs pytest, parses the JUnit
XML from the bundle, and writes a standalone HTML dashboard.

## Partial cleans

When you only want a subset, pick the lines above that apply. Common
cases:

- **Forget one URL** — in the latest run folder
  (`tests/generated/generated_<ts>/`), delete the slug's subfolder
  (`<slug>/`), the two extraction files under
  `manifest/extractions/<slug>.{json,prev.json}`, and the
  `nodes: <url>:` block in `manifest/registry.yaml`. Historical run
  folders can be left alone — the `conftest.py` filter already
  ignores them.
- **Force LLM to re-plan without re-auth** — in the latest run
  folder, delete `manifest/plans/` and `manifest/heals/`. Keep
  `.auth/` so you don't have to sign in again. Then re-run with
  `--force`. The next run will seed the empty plan caches from this
  (partially cleared) latest run and ask the LLM fresh.
- **Keep generation but re-run tests** — just re-run
  `autocoder report --run`; pytest writes a fresh `results.xml` into
  each latest bundle folder.
- **Keep only the latest run folder** — `ls -d tests/generated/generated_*`
  to see them all, then delete older ones by name. The `conftest.py`
  filter ignores older folders regardless; deletion is purely a disk
  hygiene choice. Each run folder is self-contained, so deleting
  older ones never removes data the latest run relies on.

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
