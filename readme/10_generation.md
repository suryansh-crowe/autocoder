# 10 · Generation (deterministic renderers)

Stages 5, 7a, 7b, and 2's render half are pure templates. They take a
typed plan plus the typed extraction and return a string. Zero LLM
tokens. Templates live inline in the renderer modules — they are
small, easy to diff, and always co-located with the data they
consume.

## POM render

`autocoder/generate/pom.py` writes `tests/pages/<slug>_page.py`:

```python
class DashboardPage(BasePage):
    URL = "https://app.example.com/dashboard"
    SELECTORS = {
        "search": [
            {"strategy": "test_id", "value": "search-input"},
            {"strategy": "role_name", "value": "textbox", "name": "Search assets..."},
            {"strategy": "placeholder", "value": "Search assets..."},
        ],
        "submit": [...],
    }

    def __init__(self, page: Page) -> None:
        super().__init__(page, self.SELECTORS)

    def navigate(self) -> None:
        self.page.goto(self.URL, wait_until="domcontentloaded")

    def fill_search(self, value: str) -> None:
        """Search assets..."""
        self.locate('search').fill(value)

    def click_submit(self) -> None:
        """Sign in"""
        self.locate('submit').click()
```

Action → body mapping is:

| Action            | Generated body                                            |
|-------------------|-----------------------------------------------------------|
| `click`           | `self.locate(id).click()`                                 |
| `fill`            | `self.locate(id).fill(value)`                             |
| `select`          | `self.locate(id).select_option(value)`                    |
| `check`           | `self.locate(id).check()`                                 |
| `wait`            | `self.locate(id).wait_for(state='visible')`               |
| `expect_visible`  | `expect(self.locate(id)).to_be_visible()`                 |
| `expect_text`     | `expect(self.locate(id)).to_contain_text(value)`          |
| `navigate`        | `self.navigate()` (uses class-level `URL`, `wait_until="domcontentloaded"` to match the extraction-time strategy and avoid headed-mode timeouts on SPAs whose telemetry prevents the `load` event from firing) |

## Feature render

`autocoder/generate/feature.py` writes `tests/features/<slug>.feature`.
Tiers map to Gherkin tags:

| Tier         | Tag(s)                       |
|--------------|------------------------------|
| `smoke`      | `@smoke`                     |
| `sanity`     | `@sanity`                    |
| `regression` | `@regression`                |
| `happy`      | `@smoke`                     |
| `edge`       | `@regression @edge`          |
| `validation` | `@regression @validation`    |
| `navigation` | `@regression @navigation`    |
| `auth`       | `@auth`                      |
| `rbac`       | `@regression @rbac`          |
| `e2e`        | `@e2e`                       |

A typical feature looks like:

```gherkin
Feature: Sign in
  Authenticate via the corporate login page

  Background:
    Given the user is on the sign-in page

  @smoke
  Scenario: User signs in with valid credentials
    When the user enters their email
    And the user enters their password
    And the user submits the form
    Then the dashboard is visible

  @regression @validation
  Scenario: Empty email shows an error
    When the user submits the form
    Then a validation error is shown
```

## Steps render

`autocoder/generate/steps.py` writes `tests/steps/test_<slug>.py`.
Files are deliberately named `test_*.py` so pytest's default
collection picks them up.

```python
from pytest_bdd import given, parsers, scenarios, then, when
from tests.pages.login_page import LoginPage

scenarios("login.feature")

@pytest.fixture
def login_page(page: Page) -> LoginPage:
    return LoginPage(page)

@given(parsers.parse('the user is on the Stewie AI homepage'))
def _the_user_is_on_the_stewie_ai_homepage(stewie_page: StewiePage) -> None:
    stewie_page.navigate()

@when(parsers.parse('the user checks the terms of service checkbox'))
def _the_user_checks_the_terms_of_service_checkbox(stewie_page: StewiePage) -> None:
    stewie_page.check_terms_of_service_checkbox()

@when(parsers.parse('the user does not check the terms of service checkbox'))
def _the_user_does_not_check_the_terms_of_service_check(stewie_page: StewiePage) -> None:
    pass  # intentional no-op: step text asserts a non-action (negation detected)

@then(parsers.parse('the sign-in button for Microsoft should be disabled'))
def _the_sign_in_button_for_microsoft_should_be_disable(stewie_page: StewiePage) -> None:
    expect(stewie_page.locate('sign_in_with_microsoft')).to_be_disabled()
```

### Generation rules

- One Python function per **unique** step text — same text under
  multiple keywords stacks decorators on the same function (no
  duplicate function names). `And` / `But` keywords inherit the
  previous step's keyword as Gherkin specifies.
- If the validated step has a `pom_method` AND the step text supplies
  values for every parameter the method requires, the body calls
  `fixture.<pom_method>(...args)`.
- If the step has a `pom_method` but supplies no values for a
  required parameter, the body raises
  `NotImplementedError("...expects: <param>")` instead of emitting a
  broken call.
- If the step has no `pom_method`, the renderer tries to
  **synthesize** an executable body (see below) before falling back
  to `NotImplementedError`.
- Quoted segments inside step text become `parsers.parse` arguments
  (`"foo"` → `arg0`).
- The remaining stubs are filled in by the **heal stage**
  (`autocoder heal`). See `17_heal.md`.

### Step synthesis (`_try_synthesize`)

When the LLM's feature plan leaves `pom_method=null`, the renderer
inspects the step text + the extracted element catalog + the POM
method list and picks a body in this order:

1. **Navigation** — step text matches a navigation pattern. The
   subject can be any of `is/am/are/'m/'re on|at|in` (so both "the
   user is on the login page" and "I am on the login page" match),
   and the verb can be any of `opens`, `navigates to`, `visits`,
   `goes to`, `lands on`. The object accepts `page`, `homepage`,
   `home page`, `landing`, `dashboard`, `home`, `site`, or `app`.
   Emits `fixture.navigate()`.
2. **Fuzzy POM method match** (only for `Given`/`When`/`And`/`But`
   that are not negated). Tokens of the step text are matched against
   `{method_name.split('_')}`; methods sharing ≥ 2 tokens win. The
   highest-overlap method is called.
3. **Assertion patterns** on the best-matching element:

   | Step text pattern                                          | Body |
   |------------------------------------------------------------|------|
   | `is/should be/must be checked`                             | `expect(loc).to_be_checked()` |
   | `is/should be/must be not checked` · `is unchecked`        | `expect(loc).not_to_be_checked()` |
   | `is/should be/must be visible|displayed|shown|present`     | `expect(loc).to_be_visible()` |
   | `is/should be/must be not visible` · `... hidden`          | `expect(loc).not_to_be_visible()` |
   | `is/should be/must be enabled`                             | `expect(loc).to_be_enabled()` |
   | `is/should be/must be disabled` · `... not enabled`        | `expect(loc).to_be_disabled()` |

4. **Negation no-op** — when the step is a `Given`/`When`/`And`/`But`
   that starts with `does not` / `doesn't` / `never` / `without` and
   none of the above matched, the body is:

   ```python
   pass  # intentional no-op: step text asserts a non-action (negation detected)
   ```

   This prevents the fuzzy matcher from turning "the user does NOT
   check the checkbox" into a body that actually checks the
   checkbox.
5. **Visibility fallback** for non-negated `Then` steps that name an
   element but state no specific assertion → `expect(loc).to_be_visible()`.

Fall-through: `raise NotImplementedError("Implement step: …")`.

### Placeholder quality gate

After the file is written, the orchestrator counts `NotImplementedError`
occurrences. If the count is > 0:

- `node.status = Status.NEEDS_IMPLEMENTATION` (not `COMPLETE`).
- `logger.warn("steps_incomplete", ..., placeholder_count=N)`.
- The top-level run summary becomes `run_done_with_issues` with a
  breakdown: `complete=X needs_implementation=Y failed=Z`.

This makes it impossible for a run to silently report success while
its generated tests are guaranteed to fail on `NotImplementedError`.

## Auth setup render

Covered in `06_auth_first.md`. The renderer ships four templates —
`form`, `sso_microsoft` (shared with `sso_generic`), `username_first`,
and `email_only` (shared with `magic_link` / `otp_code`) — and
`render_auth_setup(spec)` picks the right one from `spec.auth_kind`.
Same principle: deterministic template, secrets only via
`tests.settings.get_required("LOGIN_USERNAME")` /
`settings.get_optional("LOGIN_PASSWORD")` (which funnel every
`.env` read through the single `tests/settings.py` loader — no
generated file touches `os.environ` directly), single
`@pytest.mark.auth_setup` test that writes `.auth/user.json`.

## What the generator covers — and what it doesn't

"`autocoder generate` produces all relevant test cases" is not a
true statement, and the system prompts are explicit about the
boundaries. The generator trades breadth for reliability and LLM
token efficiency.

### Covered (given a successful extraction)

- **Smoke** tier: at least one scenario that exercises the primary
  happy path for the URL.
- **Happy** tier: at least one success-path scenario covering the
  most prominent form / button / link flow on the page.
- **Validation** (on by default): bounded set of validation / edge
  scenarios the LLM can justify from the extracted elements.
- Other tiers (`regression`, `edge`, `navigation`, `auth`, `rbac`,
  `e2e`) on demand via `--tier`.

The system prompt (`FEATURE_SYSTEM` in `llm/prompts.py`) caps output
at **2–6 scenarios total per URL** with **≤ 6 steps each**. The POM
plan (`POM_SYSTEM`) is capped at **≤ 20 methods per URL**. Both caps
are enforced by the validator — the LLM *cannot* produce an
exhaustive catalog even if asked.

### Not covered (today)

- **Login page tests** — the login URL is deliberately skipped; only
  the auth-setup test is produced. See `06_auth_first.md`.
- **Cross-URL workflows** — each URL is planned in isolation. Flows
  like "login → dashboard → edit profile → logout" require a URL
  list that covers each hop, plus hand-authored glue features.
- **Multi-role / RBAC** — single-user only. The hook exists in
  `AuthSpec.username_env` / `password_env`, but the orchestrator
  does not render a second `auth_setup` with a different role.
- **Negative auth scenarios** — "visit protected URL without session
  → expect redirect to login" is not generated. The LLM only sees
  the DOM of a successful extraction.
- **Behavior behind client-side state changes** — elements that only
  appear after an interaction the extraction did not perform (open
  a modal, expand a menu, paginate) are invisible to the planner.
- **Performance, accessibility, visual-regression** — out of scope.
  Add them yourself as separate pytest modules; they can import the
  generated POMs.

### Caveat for authenticated SPAs

Authenticated extraction runs with `storage_state` loaded, so
Playwright sends the session cookies on `page.goto`. That is not
the same as the URL necessarily *rendering differently*. If an
authenticated SPA still serves a consent gate, terms-acceptance
modal, or intro tour on every landing, the extracted DOM is that
gate — the LLM then writes scenarios about the gate instead of the
real application.

Two signals that this happened:

1. The post-auth fingerprint equals the pre-auth fingerprint
   (`manifest/extractions/<slug>.json` unchanged).
2. The generated scenarios mention consent / sign-in / onboarding
   even though the test runs with storage loaded.

Workaround: identify the URL the authenticated app actually lands
on (often a dashboard route that differs from the marketing URL)
and add that to your input list.

## Runtime self-heal on generated POMs

The generated POM methods do not call raw `self.locate(id).click()`.
They call `self.click(id)` (and `self.check`, `self.fill`,
`self.select`) on `BasePage`, which provides a small, deterministic
self-heal layer:

- If a click target is disabled, ``BasePage._unblock_via_consent``
  ticks visible unchecked checkboxes (native + ARIA) and retries.
  Same algorithm the auth runner uses for the SSO-button-behind-a-
  consent-checkbox pattern.
- `self.check(id)` is idempotent — no-op if already checked.
- `self.fill(id, value)` clears the field before filling.

Per-call opt-out is available: `self.click(id, heal=False)`. Use it
in explicit negative scenarios that assert a disabled state.

This is where the project leans on determinism to compensate for a
local LLM: scenario order bugs (e.g. LLM puts "click Sign in"
before "check ToS") stop breaking runs, and brittle DOMs that need
one tiny interaction before the target is clickable don't require
an `autocoder heal` call.

## Why renderers, not LLM-written code

Every Python line the LLM writes is a line that could contain a
hallucinated method name, wrong fixture, or invalid flow ordering.
Every output token is time on a slow CPU. Templates eliminate both:

- Syntax errors are structurally impossible.
- Method names come from the validated plan, which only references
  POM methods that exist.
- The renderer's behaviour is reviewable like any other code change.
- Synthesis is rule-based: a regex plus a token overlap score. No
  new variability is introduced, so generated output stays
  reproducible run to run.
