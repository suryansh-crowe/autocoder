# 12 · Running the generated tests

The generated suite is plain pytest + pytest-bdd + Playwright. Anything
you can do in those tools, you can do here. The orchestrator only
touches the suite when you ask it to (re)generate or heal.

## Generation → heal → run loop

```bash
autocoder generate <urls>          # 1. generate POMs / features / steps
autocoder heal --slug <slug>       # 2. fill NotImplementedError stubs
pytest tests/steps/test_<slug>.py  # 3. run the suite
autocoder heal --from-pytest --slug <slug>  # 4. heal runtime failures
pytest tests/steps/test_<slug>.py  # 5. re-run; repeat 4-5 if needed
```

Step 4 captures every Playwright error and asks the LLM for a
revised step body (with `failure_class` hints — disabled / modal /
wrong-kind / locator-not-found / timeout). See `17_heal.md`.

## First run after generation

If any URL in scope is authenticated, capture a session once before
running the rest of the suite:

```bash
pytest tests/auth_setup -m auth_setup
```

That writes `tests/.auth/user.json`. Subsequent runs read it
automatically (see `tests/conftest.py` — the `browser_context_args`
fixture injects `storage_state` if the file exists).

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
pytest tests/steps/test_login.py
```

Run a single scenario:

```bash
pytest tests/steps/test_login.py::test_user_signs_in_with_valid_credentials
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

`pytest --html=report.html --self-contained-html` (with
`pytest-html`) produces a single-file HTML report. Playwright's
own trace viewer (`npx playwright show-trace trace.zip`) opens
captured traces from `--tracing on`.

## CI sketch

```yaml
- run: pip install -r requirements.txt && playwright install chromium
- run: autocoder rerun                 # regenerate against current app state
- run: pytest tests/auth_setup -m auth_setup
- run: pytest -m "smoke or regression"
```

`autocoder rerun` in CI catches selector drift early — if the app
changed, the regenerated POM/features carry the new state and the
tests still pass without manual intervention.

## Why generated steps may raise NotImplementedError

The feature-plan validator nulls out `pom_method` references the LLM
invented. The renderer then emits a step body of:

```python
raise NotImplementedError("Implement step: <step text>")
```

This is intentional. The system never silently passes a step it
could not bind to a real method. When you see one, either:

1. Add the corresponding method to the generated POM (and re-run
   generation so the validator finds it), or
2. Replace the body with whatever assertion the step actually means.

Either way, the failure is loud and the fix is local.
