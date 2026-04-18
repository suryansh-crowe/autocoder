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
        self.page.goto(self.URL)

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
| `navigate`        | `self.navigate()` (uses class-level `URL`)                |

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

@when(parsers.parse('the user enters their email'))
def _the_user_enters_their_email(login_page: LoginPage) -> None:
    login_page.fill_email()

@then(parsers.parse('the dashboard is visible'))
def _the_dashboard_is_visible(login_page: LoginPage) -> None:
    raise NotImplementedError("Implement step: the dashboard is visible")
```

Generation rules:

- One `@given` / `@when` / `@then` per **unique** step text. Repeated
  text across scenarios shares a definition.
- If the validated step has a `pom_method`, the body calls
  `fixture.<pom_method>(...args)`.
- If it does not (e.g. an assertion the LLM could not map), the body
  raises `NotImplementedError("Implement step: …")`. The step never
  silently passes.
- Quoted segments inside step text become `parsers.parse` arguments
  (`"foo"` → `arg0`).

## Auth setup render

Covered in `06_auth_first.md`. Same principle: deterministic template,
secrets only via `os.environ.get(...)`, single `@pytest.mark.auth_setup`
test that writes `storage_state` to `tests/.auth/user.json`.

## Why renderers, not LLM-written code

Every Python line the LLM writes is a line that could contain a
hallucinated method name, wrong fixture, or invalid flow ordering.
Every output token is time on a slow CPU. Templates eliminate both:

- Syntax errors are structurally impossible.
- Method names come from the validated plan, which only references
  POM methods that exist.
- The renderer's behaviour is reviewable like any other code change.

This is the same trade-off the original architecture
(`info/06_optimized_architecture.md` in the legacy notes) settled on.
