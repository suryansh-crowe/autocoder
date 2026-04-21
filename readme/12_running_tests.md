# 12 · Running the generated tests

The generated suite is plain pytest + pytest-bdd + Playwright. Anything
you can do in those tools, you can do here. The orchestrator only
touches the suite when you ask it to (re)generate or heal.

## Generation → heal → run loop

```bash
autocoder generate <urls>                          # 1. generate POMs / features / steps into tests/generated/generated_<ts>/
autocoder heal --slug <slug>                       # 2. fill NotImplementedError stubs
pytest tests/generated/generated_*/<slug>/test_<slug>.py   # 3. run the suite
autocoder heal --from-pytest --slug <slug>         # 4. heal runtime failures
pytest tests/generated/generated_*/<slug>/test_<slug>.py   # 5. re-run; repeat 4-5 if needed
```

The `tests/generated/conftest.py` filter keeps only the newest run
folder per slug, so the `generated_*` glob above collects exactly one
test file per slug even when historical run folders are still on
disk.

Step 4 captures every Playwright error and asks the LLM for a
revised step body (with `failure_class` hints — disabled / modal /
wrong-kind / locator-not-found / timeout). See `17_heal.md`.

## First run after generation

For form and Microsoft SSO flows, `autocoder generate` has already
performed the login in-process and written `.auth/user.json`. You can
jump straight to `pytest`.

The generated `tests/auth_setup/test_auth_setup.py` is **excluded from
default pytest runs** via `addopts = -m "not auth_setup"` in
`pytest.ini`. That avoids two annoyances:

* SSO / passwordless tenants fail the auth-setup test at
  `_need("LOGIN_PASSWORD")` even though the session is already
  captured — irrelevant noise in the CI / autoheal report.
* Re-running the auth-setup test when `.auth/user.json` already
  exists is wasted work.

If you DO want to rerun the auth flow explicitly (credentials
changed, session expired, you deleted `.auth/user.json`), use:

```bash
pytest -m auth_setup
```

The rendered auth-setup test has a first-line `_skip_if_session_captured()`
guard — if `.auth/user.json` is still present, the test exits as
**skipped** (green), not failed. Delete the file (or run
`autocoder auth-reset`) to force a fresh capture.

If the run log ended with `auth_session_awaiting_external` — magic
link, OTP, or MFA that the runner can't complete on its own — finish
it headful once:

```bash
HEADLESS=false pytest tests/auth_setup -m auth_setup
```

The rendered setup test was picked from the template that matches the
detected `auth_kind`, so it already knows whether to wait for a
password field, an email link, or a code. `.auth/user.json` is
written at the end; the `browser_context_args` fixture in
`tests/conftest.py` loads it for every subsequent test.

Session expired? Delete `.auth/user.json` and rerun `autocoder
generate` — the auth runner re-authenticates.

## Tier markers

Every generated scenario carries a tier tag. Tag → marker mapping is
defined in `pytest.ini`:

| Marker         | Selects scenarios tagged with                               |
|----------------|-------------------------------------------------------------|
| `smoke`        | `@smoke`                                                    |
| `sanity`       | `@sanity`                                                   |
| `regression`   | `@regression`                                               |
| `validation`   | `@validation`                                               |
| `navigation`   | `@navigation`                                               |
| `edge`         | `@edge`                                                     |
| `auth`         | `@auth`                                                     |
| `auth_setup`   | The auth setup test only                                    |
| `rbac`         | `@rbac`                                                     |
| `e2e`          | `@e2e`                                                      |

Run by tier:

```bash
pytest -m smoke
pytest -m "regression and not edge"
pytest -m "auth or smoke"
```

Run a single feature:

```bash
pytest tests/generated/generated_*/login/test_login.py
```

Run a single scenario:

```bash
pytest tests/generated/generated_*/login/test_login.py::test_user_signs_in_with_valid_credentials
```

(pytest-bdd derives the test function name from the scenario title.)

## Useful flags

| Need                           | Flag                                 |
|--------------------------------|--------------------------------------|
| Show browser                   | `--headed`                           |
| Slow each step down            | `--slowmo 200`                       |
| Use a different browser        | `--browser firefox`                  |
| Debug a failure interactively  | `--pdb`                              |
| Capture trace                  | `--tracing on`                       |
| Different base URL             | `BASE_URL=... pytest`                |
| Force fresh storage_state      | Delete `tests/.auth/user.json` and re-run auth setup |

## Reports

### Built-in HTML dashboard (`autocoder report`)

```bash
autocoder report --run --html manifest/report.html     # run pytest, then write HTML
autocoder report --html manifest/report.html           # reuse existing JUnit XML
autocoder report --run --json > report.json            # machine-readable output
```

Produces a standalone, dark-themed HTML file showing, per URL:

* detected **UI components** as coloured chips (search, chat, forms,
  nav, buttons, choices, data, pagination) — computed from the
  extraction by `build_ui_inventory()`;
* **every scenario** with its tier tags and pass / fail / unknown
  result;
* **overall totals** and a pass rate.

`--run` invokes pytest against every slug's newest-bundle test file
and writes a fresh `tests/generated/<run>/<slug>/results.xml` in
place first (legacy flat layouts still fall back to
`manifest/runs/<slug>.xml`). Without `--run` the
report reads whatever XML is already on disk (slugs that have
never been tested show `unknown`). `--open/--no-open` controls
whether the HTML file is opened in the default browser
(`--open` by default when `--html` is used).

### Automatic report after every `pytest` session

`tests/conftest.py` ships two hooks that make plain `pytest` work
the same way as `autocoder report --run`:

* **`pytest_configure`** — if you didn't pass `--junit-xml`, it
  auto-sets `--junit-xml=manifest/runs/_pytest_session.xml` so the
  raw XML always exists.
* **`pytest_sessionfinish`** — splits that XML into per-slug files
  (`manifest/runs/<slug>.xml`) and regenerates
  `manifest/report.html` using `autocoder.report.render_html_report`.
  Prints `[autocoder-report] N slug(s) updated → <path>` at the
  bottom of the pytest output.

Opt out with `AUTOCODER_AUTOREPORT=false` in `.env` (useful for
downstream consumers who only want to run the generated suite and
don't have the `autocoder` package installed). Any explicit
`--junit-xml=<path>` you pass is preserved; the hook only wires a
default.

### Third-party options

`pytest --html=report.html --self-contained-html` (with
`pytest-html`) produces an alternative single-file HTML report.
Playwright's own trace viewer (`npx playwright show-trace trace.zip`)
opens captured traces from `--tracing on`.

## CI sketch

```yaml
- run: pip install -r requirements.txt && playwright install chromium
- run: autocoder rerun                              # regenerate against current app state
- run: pytest tests/auth_setup -m auth_setup        # refresh .auth/user.json if needed
- run: pytest -m "smoke or regression"              # execute suite (auto-report fires on finish)
- run: autocoder report --html manifest/report.html # optional: rebuild the HTML with any extra JUnit
- uses: actions/upload-artifact@v4
  with:
    name: autocoder-report
    path: manifest/report.html
```

`autocoder rerun` in CI catches selector drift early — if the app
changed, the regenerated POM/features carry the new state and the
tests still pass without manual intervention. `manifest/report.html`
is produced automatically by the pytest run above; the explicit
`autocoder report --html` line is only necessary if you disabled
`AUTOCODER_AUTOREPORT`.

## Runtime self-heal on generated actions

Generated POM methods call `self.click(id)` / `self.check(id)` /
`self.fill(id, v)` / `self.select(id, v)` on `tests/pages/base_page.py` (the hand-written base),
not raw Playwright methods. `BasePage` provides a small deterministic
self-heal layer:

- **Disabled click target** — ticks visible unchecked consent
  checkboxes (native `input[type=checkbox]` + ARIA
  `[role=checkbox][aria-checked=false]`) and retries the click.
  Covers the "Sign in is disabled until Terms is checked" pattern.
- **`self.check(id)`** — idempotent; no-op if the box is already
  checked.
- **`self.fill(id, v)`** — clears the field before filling.

Negative scenarios that deliberately assert a disabled state should
opt out: `self.click(id, heal=False)`. See `10_generation.md` for
the full contract.

## Why generated steps may raise NotImplementedError

The renderer tries three things before giving up on a step:

1. Call the bound `pom_method` if the validator kept it (or
   close-match rebound it).
2. Synthesize an executable body from the step text — navigation,
   assertion patterns, or a negation no-op. See `10_generation.md`.
3. Emit `raise NotImplementedError("Implement step: <step text>")`.

Only when all three miss do you see a placeholder. The URL is then
marked `needs_implementation` and the end-of-run summary becomes
`run_done_with_issues`. When you see one, either:

1. Run `autocoder heal --slug <slug>` to ask the LLM for a single
   validated statement; or
2. Add the corresponding method to the generated POM (and re-run
   generation so the validator finds it); or
3. Replace the body by hand with whatever the step actually means.

Either way, the failure is loud and the fix is local.
