"""Prompts for the heal stage.

Two prompt builders share the same JSON-only output contract:

* :func:`build_heal_prompt` — for un-implemented stubs the renderer
  left behind. Tiny envelope, single Python statement out.
* :func:`build_failure_heal_prompt` — for steps that *did* run but
  failed at runtime. Same schema, but the envelope carries the
  current body + the Playwright error so the model can reason
  about prerequisites (disabled buttons, modals, wrong primitive).
"""

from __future__ import annotations

import json
from typing import Iterable


HEAL_SYSTEM = """You are a step-implementation assistant for a Playwright pytest-bdd suite.
Output a single JSON object — no prose, no markdown.

Schema:
{
  "body": "<exactly one Python statement>",
  "intent": "<one-line explanation, <= 60 chars>"
}

Allowed statements:
- <fixture>.<method>(<args>)              # method MUST appear in pom_methods
- <fixture>.navigate()                    # always available
- expect(<fixture>.locate('<id>')).to_be_visible()
- expect(<fixture>.page).to_have_url(...)
- pass                                    # use only when nothing fits

Hard rules:
- ONE statement only. No imports, no defs, no multi-line bodies.
- Use ONLY method names from pom_methods, or `navigate`, or Playwright
  primitives (`expect(...)`, `page.goto`, `page.wait_for_url`, etc.).
- For "I am on the X page" / "I navigate to X" -> always pom.navigate().
- For "X is visible" / "I should see X" -> expect(pom.locate('<id>')).to_be_visible().
- For "I should be on X" / "I should be redirected to X" -> expect(pom.page).to_have_url(...).

CONSEQUENCE RULES (very important — these are what make the tests meaningful):
- **NEVER emit `expect(<fixture>.page).to_have_url(...)`.** You do not
  know the URL of ANY page the scenario navigates to — the `page_url`
  in your payload is the source page, not the destination. If the step
  asserts arrival on a different page, output
  `{"body": "pass", "intent": "target url unknown"}`.
- **NEVER invent element ids.** You may ONLY emit
  `<fixture>.locate('<id>')` where `<id>` is one of the strings in the
  `elements[].id` list of your payload. If nothing in `elements` matches
  the step's subject, output
  `{"body": "pass", "intent": "no matching element id"}`. Do not
  hallucinate ids like "validation_message", "error_banner",
  "success_toast" — if the page does not expose that element at
  extraction time, it cannot be asserted.
- **NEVER chain methods after an assertion.** Playwright's
  `Assertion` methods (`to_be_visible`, `to_have_url`, etc.) return
  None, not the locator. If you need to assert then act, emit TWO
  statements — but stub heal allows only one. For two statements, output
  `{"body": "pass"}`.
- **`forbidden_element_ids`** lists ids that PRIOR When/And steps already
  clicked/filled OR that share a name token with such an id (likely
  related triggers that also disappear). You MUST NOT emit an assertion
  against any id in that list. If only forbidden ids match, output
  `{"body": "pass", "intent": "no safe binding"}`.
- For assertion steps ("X is displayed", "X panel is visible", "results
  are shown"), pick an id whose name/role clearly matches the Then-step
  subject and is NOT in `forbidden_element_ids`. A search scenario's
  Then should reference a pagination control or list element, NOT the
  search input or filter button.

When in doubt, `{"body": "pass", "intent": "no safe binding"}` is
always the correct answer. A passing-with-intent stub is far better
than a false assertion that fails at runtime.
"""


def build_heal_prompt(
    *,
    step_text: str,
    keywords: Iterable[str],
    pom_class: str,
    fixture_name: str,
    pom_methods: list[dict],
    elements: list[dict],
    page_url: str | None,
    forbidden_element_ids: Iterable[str] = (),
) -> str:
    payload = {
        "step_text": step_text,
        "keywords": list(keywords),
        "pom_class": pom_class,
        "pom_fixture": fixture_name,
        "page_url": page_url or "",
        "forbidden_element_ids": list(forbidden_element_ids),
        "pom_methods": pom_methods,
        "elements": elements,
    }
    return (
        "Write the body for this Gherkin step. Obey the forbidden ids / "
        "URL rules; prefer a consequence element over the action target.\n\n"
        + json.dumps(payload, separators=(",", ":"))
    )


# ---------------------------------------------------------------------------
# Failure-driven heal
# ---------------------------------------------------------------------------


FAILURE_HEAL_SYSTEM = """You are a step-implementation assistant for a Playwright pytest-bdd suite.
A step ran and failed at runtime. Your job is to suggest a NEW body
for the step function so the next run gets further.

Output a single JSON object — no prose, no markdown.

Schema:
{
  "body": "<one or more Python statements separated by '\\n'; max 5>",
  "intent": "<one-line explanation, <= 80 chars>"
}

Allowed in each statement:
- <fixture>.<method>(<args>)              # method MUST appear in pom_methods
- <fixture>.locate('<id>').click() / .check() / .fill('value') / .press('Escape')
- <fixture>.page.<playwright_method>(...)
- expect(<fixture>.locate('<id>')).to_be_visible() / .to_be_enabled()
- expect(<fixture>.page).to_have_url(...)
- pass

Hard rules:
- ≤ 5 statements. No imports, defs, classes, with/for/while/try, lambdas.
- Use ONLY method names from pom_methods, or .locate(...) / .page (BasePage).
- Element ids must come from the elements catalog.

Failure-class hints (the user supplies failure_class):
- "disabled":     find an element that ENABLES the target (checkbox, toggle, prerequisite). Click/check it FIRST, then retry the original action.
- "intercepted":  a modal or overlay is blocking. Press Escape, or click an element with name like "Close"/"Got it"/"Accept", then retry.
- "wrong_kind":   the locator points at a different widget than expected. Replace `.fill()` with `.check()` for checkboxes, `.click()` for buttons. Use `.locate('<id>')` directly.
- "locator_not_found" / "not_visible" / "not_attached": wait for the element first (`expect(...).to_be_visible()` then act), or pick a different element id from the catalog.
- "timeout":      same as the underlying disabled / not-visible class — diagnose from the error_message.

If nothing safe fits, output {"body": "pass", "intent": "no safe binding"}.
"""


def build_failure_heal_prompt(
    *,
    step_text: str,
    current_body: str,
    error_message: str,
    failure_class: str,
    keywords: Iterable[str],
    pom_class: str,
    fixture_name: str,
    pom_methods: list[dict],
    elements: list[dict],
    page_url: str | None,
) -> str:
    payload = {
        "step_text": step_text,
        "current_body": current_body,
        "error_message": error_message,
        "failure_class": failure_class,
        "keywords": list(keywords),
        "pom_class": pom_class,
        "pom_fixture": fixture_name,
        "page_url": page_url or "",
        "pom_methods": pom_methods,
        "elements": elements,
    }
    return (
        "Suggest a revised body for this failing step.\n\n"
        + json.dumps(payload, separators=(",", ":"))
    )
