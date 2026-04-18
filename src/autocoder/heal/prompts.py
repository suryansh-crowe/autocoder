"""Prompts for the heal stage.

One LLM call per stub. Input is a tiny JSON envelope; output must be a
single safe Python statement. The schema is enforced by
``autocoder.heal.validator``; the prompt only nudges the model toward
the validator's accept set.
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
- When unsure, output {"body": "pass", "intent": "no safe binding"}.
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
) -> str:
    payload = {
        "step_text": step_text,
        "keywords": list(keywords),
        "pom_class": pom_class,
        "pom_fixture": fixture_name,
        "page_url": page_url or "",
        "pom_methods": pom_methods,
        "elements": elements,
    }
    return (
        "Write the body for this Gherkin step.\n\n"
        + json.dumps(payload, separators=(",", ":"))
    )
