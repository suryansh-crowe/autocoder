"""Prompts for the heal stage.

Two prompt builders share the same JSON-only output contract:

* :func:`build_heal_prompt` — for test functions whose body is a single
  ``NotImplementedError`` stub (produced when the codegen prompt could
  not map a scenario statement to a safe Playwright call). The model
  emits a list of Python statements to run.
* :func:`build_failure_heal_prompt` — for Playwright tests that *did*
  run but failed at runtime. Same schema, but the envelope carries
  the current body + the Playwright error so the model can reason
  about prerequisites (disabled buttons, modals, wrong primitive).

Both prompts operate at the **test-function** level: one LLM call
produces the full list of body statements for a failing test.
"""

from __future__ import annotations

import json
from typing import Iterable


HEAL_SYSTEM = """You fill in the body of a pytest Playwright test function.
Output a single JSON object — no prose, no markdown.

Schema:
{
  "statements": ["<one Python statement per entry, max 20>"],
  "intent": "<one-line explanation, <= 80 chars>"
}

Allowed in each statement (ONE statement per list entry, no imports or defs):
- <fixture>.<method>(<args>)                # method MUST appear in pom_methods
- <fixture>.navigate()                      # always available (BasePage)
- <fixture>.locate('<id>').click() / .check() / .fill('<value>') / .press('<key>')
- <fixture>.page.<playwright_method>(...)
- expect(<fixture>.locate('<id>')).to_be_visible() / .not_to_be_visible() / .to_be_checked() / .to_be_enabled() / .to_be_disabled() / .to_contain_text('<str>')
- expect(<fixture>.page).to_have_url(<str_or_regex>)
- pass                                      # only when nothing else fits

Hard rules:
- ≤ 20 statements. No imports, defs, classes, with/for/while/try, lambdas.
- First statement MUST be `<fixture>.navigate()` — the page must be loaded.
- Use ONLY method names from pom_methods, or .locate(...) / .page (BasePage).
- Element ids must come from the elements catalog.
- For "user is on the X page" — emit `<fixture>.navigate()`.
- For "X is visible" — emit `expect(<fixture>.locate('<id>')).to_be_visible()`.
- For URL assertions — emit `expect(<fixture>.page).to_have_url(<value>)`.

When no safe body fits, output {"statements": ["pass"], "intent": "no safe binding"}.
"""


def build_heal_prompt(
    *,
    test_name: str,
    scenario_title: str,
    pom_class: str,
    fixture_name: str,
    pom_methods: list[dict],
    elements: list[dict],
    page_url: str | None,
) -> str:
    payload = {
        "test_name": test_name,
        "scenario_title": scenario_title,
        "pom_class": pom_class,
        "pom_fixture": fixture_name,
        "page_url": page_url or "",
        "pom_methods": pom_methods,
        "elements": elements,
    }
    return (
        "Write the body statements for this Playwright test function.\n\n"
        + json.dumps(payload, separators=(",", ":"))
    )


# ---------------------------------------------------------------------------
# Failure-driven heal
# ---------------------------------------------------------------------------


FAILURE_HEAL_SYSTEM = """You rewrite the body of a failing pytest Playwright test function.
A test ran and failed at runtime. Replace the ENTIRE body with statements that will pass.

Output a single JSON object — no prose, no markdown.

Schema:
{
  "statements": ["<one Python statement per entry, max 20>"],
  "intent": "<one-line explanation, <= 80 chars>"
}

Allowed in each statement (same as stub heal):
- <fixture>.<method>(<args>)                # method MUST appear in pom_methods
- <fixture>.navigate()                      # always available (BasePage)
- <fixture>.locate('<id>').click() / .check() / .fill('<value>') / .press('<key>')
- <fixture>.page.<playwright_method>(...)
- expect(<fixture>.locate('<id>')).to_be_visible() / .not_to_be_visible() / .to_be_checked() / .to_be_enabled() / .to_be_disabled() / .to_contain_text('<str>')
- expect(<fixture>.page).to_have_url(<str_or_regex>)
- pass

Hard rules:
- ≤ 20 statements. No imports, defs, classes, with/for/while/try, lambdas.
- First statement MUST be `<fixture>.navigate()`.
- Use ONLY method names from pom_methods. Element ids must come from the elements catalog.
- Do not re-emit the exact failing statement — either change the primitive, add a
  prerequisite (checkbox, modal dismiss), or pick a different element id.

Failure-class hints (the user supplies failure_class):
- "disabled":     add a prerequisite click/check (enabling checkbox, toggle) BEFORE retrying
                  the original action.
- "intercepted":  a modal/overlay is blocking. Dismiss it first (`page.keyboard.press('Escape')`
                  or click the close button) then retry the action.
- "wrong_kind":   replace `.fill()` with `.check()` for checkboxes, `.click()` for buttons,
                  or use `.locate('<id>')` directly.
- "locator_not_found" / "not_visible" / "not_attached":
                  wait for the element first (`expect(...).to_be_visible()` then act), or pick
                  a different element id from the catalog.
- "timeout":      same as disabled / not_visible — diagnose from the error_message.

If nothing safe fits, output {"statements": ["pass"], "intent": "no safe binding"}.
"""


def build_failure_heal_prompt(
    *,
    test_name: str,
    scenario_title: str,
    current_body: str,
    error_message: str,
    failure_class: str,
    pom_class: str,
    fixture_name: str,
    pom_methods: list[dict],
    elements: list[dict],
    page_url: str | None,
) -> str:
    payload = {
        "test_name": test_name,
        "scenario_title": scenario_title,
        "current_body": current_body,
        "error_message": error_message,
        "failure_class": failure_class,
        "pom_class": pom_class,
        "pom_fixture": fixture_name,
        "page_url": page_url or "",
        "pom_methods": pom_methods,
        "elements": elements,
    }
    return (
        "Suggest a revised body (list of statements) for this failing test.\n\n"
        + json.dumps(payload, separators=(",", ":"))
    )
