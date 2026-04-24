# 17 · Heal — fill stubs and revise failing step bodies via the LLM

`autocoder heal` runs in two modes against your generated step
files. Both validate the LLM's suggestions against the POM's real
method list before writing anything.

| Mode | Command | What it heals |
|------|---------|---------------|
| **Stub heal** (default) | `autocoder heal` | `raise NotImplementedError("Implement step: …")` bodies the renderer left when it couldn't bind a step. |
| **Failure heal** | `autocoder heal --from-pytest` | Step bodies whose pytest run failed at runtime. Reads the Playwright error and asks the LLM for a revised body. |

Common flags:

```bash
autocoder heal --slug login           # restrict to test_login.py
autocoder heal --dry-run              # preview suggestions; do not write
autocoder heal --force                # bypass cache; re-call the LLM
autocoder heal --junit-xml report.xml # heal from an existing JUnit report
```

## What it does

1. **Scans** `tests/steps/test_*.py` for the renderer's exact stub
   shape (1-statement body whose only statement is
   `raise NotImplementedError("Implement step: …")`). Hand-edited
   bodies and multi-statement bodies are **left alone**.
2. **Loads context** for each stub: the cached POM plan
   (`manifest/plans/<slug>_page.pom.<fp>.json`) and the extraction
   snapshot (`manifest/extractions/<slug>.json`). The LLM sees the
   real method list and the real element catalog — never a guess.
3. **One LLM call per stub** asking for a single Python statement.
   Prompt + response are JSON; ~250 in / ~50 out per stub.
4. **Validates** the suggestion via AST. Rejected if it is not
   exactly one statement, references a non-existent POM method, or
   contains any of: `import`, `def`, `class`, `with`, `for`,
   `while`, `try`, `lambda`, comprehensions, exec/eval. Rejected
   suggestions are logged as `heal_invalid_body` and the stub is
   left in place.
5. **Applies** the validated body by line-replacement, then
   re-parses the whole file as a sanity check. If the rewritten
   file fails to parse, the change is aborted and the original is
   kept untouched.
6. **Caches** by `(slug, step_text, page_fingerprint, pom_method_set)`
   under `manifest/heals/`. Reruns of unchanged stubs spend zero
   tokens.

## What the LLM is allowed to emit

The validator's accept set (anything else is rejected and logged):

| Pattern | Use |
|---------|-----|
| `<fixture>.<method>(...)` where method is in pom_methods | bind to a generated POM method |
| `<fixture>.navigate()` | go to the page's canonical URL |
| `<fixture>.locate('<id>')` | resolve via the self-healing locator |
| `<fixture>.page.<playwright_method>(...)` | drop down to raw Playwright |
| `expect(<fixture>.locate(...)).to_be_visible()` etc. | Playwright assertion |
| `expect(<fixture>.page).to_have_url(...)` | URL assertion |
| `pass` | model has nothing useful to say |

`pass` is allowed but discouraged — the system prompt instructs the
model to use it only when nothing fits.

## Typical workflow

```bash
# generate the suite
autocoder generate

# run the suite once — see which stubs failed
pytest tests/steps/test_login.py

# heal the stubs (one LLM call per stub, ~200s each on Phi-4 CPU)
autocoder heal --slug login

# rerun
pytest tests/steps/test_login.py
```

If the model's heal still doesn't match what your app expects, edit
the body by hand. The next `autocoder heal --slug login` will see
your edit (it's no longer the renderer-shape stub) and skip it. Your
edit is preserved across reruns.

## Logs and tokens

Every heal call lands in the per-invocation log file
(`manifest/logs/<ts>-heal.log`) with the same `llm_call` schema as
POM/feature plans, plus a `purpose` field shaped
`heal:<slug>:<function_name>` (or `heal_fail:...` for failure heal)
so you can filter:

```bash
jq -r 'select(.event=="llm_call" and (.purpose|startswith("heal"))) |
       "\(.purpose)  in=\(.in_tokens) out=\(.out_tokens) cached=\(.cached)"' \
   manifest/logs/*-heal.log
```

Stage-level events: `heal_start`, `heal_context_loaded` per slug,
`heal_applied` / `heal_dry_run` / `heal_invalid_body` per stub,
`heal_done` at the end.

## Failure-heal mode (`--from-pytest`)

Runs pytest with `--junit-xml=manifest/heals/last-pytest.xml`,
parses the XML, and for every failure builds a richer prompt:

| Field sent to LLM | Source |
|-------------------|--------|
| `step_text`       | `parsers.parse(...)` decorator on the failing step |
| `current_body`    | The body that just failed |
| `error_message`   | Playwright's first line (e.g. `Locator.click: Timeout 30000ms exceeded`) |
| `failure_class`   | Heuristic: `disabled` / `intercepted` / `wrong_kind` / `not_visible` / `not_attached` / `locator_not_found` / `timeout` / `other` |
| `pom_methods` + `elements` + `page_url` | Same context the stub-heal prompt uses |

The validator runs with `max_statements=5` so the model can suggest
a prerequisite + the original action in one body, e.g.:

```python
login_page.locate('agreement').check()
login_page.click_sign_in_with_microsoft()
```

Cache key is `(slug, step_text, failure_class, error_message,
fingerprint)` — a different failure on the same step is treated as
a fresh problem, but identical failures on rerun are free.

## Safety guarantees

- **Hand edits survive.** Stub heal only touches the renderer's
  exact 1-statement `raise NotImplementedError("Implement step: …")`
  shape. Failure heal only targets the function names that pytest
  reported as failing, and only after the body has been validated.
- **No unsafe code is ever written.** The validator rejects
  imports, function/class definitions, lambdas, comprehensions,
  syntax errors, and any reference to a POM method that doesn't
  actually exist. Stub heal allows 1 statement; failure heal allows
  ≤ 5 — every statement is checked.
- **No partial writes.** If the rewritten file does not re-parse,
  the original file is left untouched (the applier raises before
  any disk write).
- **No secrets in suggestions.** The heal prompts only ship step
  text + POM method names + element catalog + (for failure heal)
  the Playwright error message — never `.env` values.

## CLI options

| Flag | Meaning |
|------|---------|
| `--slug <name>` | Restrict to `tests/steps/test_<slug>.py` only. |
| `--dry-run` | Show suggestions in the result table; do not write any file. |
| `--force` | Bypass the on-disk cache; re-call the LLM for every target. |
| `--from-pytest` | Run pytest first and heal whatever failed. |
| `--junit-xml PATH` | Heal from an existing JUnit-XML report instead of running pytest. |

## Failure categorisation in the report

Once pytest has run at least once, `autocoder report --run` and the
auto-generated `manifest/report.html` slot every failure into one of
four buckets so the right team can pick it up:

| Category | What it means | Who fixes it |
|---|---|---|
| **Frontend** (`disabled`, `intercepted`, `wrong_kind`, `not_visible`) | The UI behaved differently than the scenario expected. A real product bug. | App team |
| **Script** (`locator_not_found`, `not_attached`, plus any `ImportError` / `SyntaxError` / `NameError` / `AttributeError` / `TypeError` / …) | The test code is wrong — stale selector, bad POM, heal mis-fire. | Autocoder maintainer / rerun heal |
| **Environment** (`timeout`) | Opaque timeout, flake, slow network, expired session. | Retry / investigate |
| **Other** | Assertion failures without a known bucket token. | Manual triage |

The report's **Failure ownership** cards show per-category totals
at the top, followed by one collapsible table per category (frontend
+ script expanded by default). The rich-text `autocoder report`
summary ends with a `Frontend / Script / Env / Other` row when any
test failed. JSON output from `autocoder report --json` carries
`failure_category_totals` plus a per-scenario `failure_class` /
`category` so CI can route tickets programmatically.

## Playwright traces for failing tests

`AUTOCODER_TRACE=true` (default on) tells the `context` fixture in
`tests/conftest.py` to record a Playwright trace for every test.
Passing tests stop tracing without writing; failing tests dump to
`manifest/traces/<timestamp>_<nodeid>.zip` and the path is printed
in yellow at end-of-test.

View one:

```bash
npx playwright show-trace manifest/traces/1745241832_tests_steps_test_catalog_py__test_search.zip
```

You get the full step-by-step timeline: every action, DOM snapshot
before + after, network tab, console logs, and the exact locator
state at each frame.

Turn tracing off for faster runs:

```env
AUTOCODER_TRACE=false
```
