"""Prompt builders.

Two prompts are sent to the model. Both ask for *only* a JSON object:

* :func:`build_pom_prompt`     — page object plan
* :func:`build_feature_prompt` — Gherkin feature plan

Both prompts contain just the compact element catalog (id + role + name
+ kind), nothing else. No DOM dumps, no full a11y trees, no source
code. That is what keeps the input under ~1k tokens for typical pages.
"""

from __future__ import annotations

import json
from typing import Iterable

from autocoder.models import Element, PageExtraction


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

POM_SYSTEM = """You are a planner for a Playwright test generator.
Output a single JSON object — no prose, no markdown.

Schema:
{
  "class_name": "<CamelCase>Page",
  "fixture_name": "<snake_case_page>",
  "methods": [
    {"name": "<snake_case>", "intent": "<<= 60 chars>",
     "element_id": "<id from elements>",
     "action": "click|fill|check|select|navigate|wait|expect_visible|expect_text",
     "args": ["<arg names if action needs values>"]}
  ]
}

Rules:
- Use ONLY element ids that appear in the input list.
- Choose `action` based on element kind:
    button/link/tab/menuitem -> click
    input/textarea            -> fill   (args=["value"])
    select                    -> select (args=["value"])
    checkbox/radio            -> check
    heading                   -> expect_visible
- Method names: short verb_object form (click_login, fill_email).
- 1 method per element you intend to expose. Skip purely decorative ones.
- Keep total methods <= 20.
"""

FEATURE_SYSTEM = """You are a planner for a Playwright BDD generator.
Output a single JSON object — no prose, no markdown.

Schema:
{
  "feature": "<one line>",
  "description": "<one line>",
  "background": [{"keyword": "Given", "text": "<step text>", "pom_method": "<method>", "args": []}],
  "scenarios": [
    {
      "title": "<one line>",
      "tier": "smoke|sanity|regression|happy|edge|validation|navigation|auth|rbac|e2e",
      "steps": [
        {"keyword": "Given|When|Then|And", "text": "<step text>",
         "pom_method": "<method or null>", "args": []}
      ]
    }
  ]
}

Rules:
- Step text must be plain English, present tense, no Gherkin keyword inside the text.
- Reference ONLY pom methods that appear in the provided list. If a step is a pure
  assertion that does not map to a method, set pom_method to null.
- Generate at minimum: 1 smoke + 1 happy. Add validation/edge/navigation as warranted.
- Avoid duplicate scenarios. Keep each scenario <= 6 steps.
- Total scenarios: 2-6.
"""


# ---------------------------------------------------------------------------
# User prompt builders
# ---------------------------------------------------------------------------


def _compact_elements(elements: Iterable[Element]) -> list[dict[str, str]]:
    """Element catalog stripped to the fields the model needs."""
    out: list[dict[str, str]] = []
    for e in elements:
        item: dict[str, str] = {"id": e.id, "kind": e.kind}
        if e.name:
            item["name"] = e.name
        if e.role and e.role != e.kind:
            item["role"] = e.role
        out.append(item)
    return out


def build_pom_prompt(extraction: PageExtraction, *, page_class: str, fixture_name: str) -> str:
    payload = {
        "url": extraction.final_url,
        "title": extraction.title,
        "class_name": page_class,
        "fixture_name": fixture_name,
        "elements": _compact_elements(extraction.elements),
        "forms": [f.model_dump(mode="json") for f in extraction.forms],
    }
    return (
        "Build a POM plan for this page.\n\n"
        + json.dumps(payload, separators=(",", ":"))
    )


def build_feature_prompt(
    extraction: PageExtraction,
    *,
    pom_method_names: list[str],
    requested_tiers: list[str],
) -> str:
    payload = {
        "url": extraction.final_url,
        "title": extraction.title,
        "headings": extraction.headings,
        "pom_methods": pom_method_names,
        "elements": _compact_elements(extraction.elements),
        "tiers": requested_tiers,
    }
    return (
        "Build a feature plan covering the requested tiers. "
        "Reuse existing pom_methods where possible.\n\n"
        + json.dumps(payload, separators=(",", ":"))
    )
