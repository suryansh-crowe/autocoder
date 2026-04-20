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

Core rules:
- Step text must be plain English, present tense, no Gherkin keyword inside the text.
- Reference ONLY pom methods that appear in the provided list. If a step is a pure
  assertion that does not map to a method, set pom_method to null.
- Avoid duplicate scenarios. Keep each scenario <= 6 steps. Total scenarios: 3-8.

COMPONENT-AWARE TEST COVERAGE
-----------------------------
The user prompt carries a `ui_inventory` block that counts the kinds of
components actually rendered on the page. Use it to decide which test
types to generate. Each present component MUST produce at least one
dedicated scenario. Absent components MUST NOT produce scenarios.

Heuristics:
- search boxes (inventory.search > 0)
  → generate a search scenario: enter a query, submit/filter, assert a
    results region or list is visible (NOT the search box itself).
- chat / ask / question textboxes (inventory.chat > 0)
  → generate a chat interaction scenario: type a prompt, send, assert
    the response / message area becomes visible (NOT the input).
- forms (inventory.forms > 0)
  → generate TWO scenarios: (a) valid submission happy path, and
    (b) validation — submit empty / invalid input and assert the
    error/helper text is visible.
- navigation links/tabs (inventory.nav > 0)
  → generate a navigation scenario: click a distinct nav target and
    assert the URL changed OR a landmark heading for that target
    appears.
- action buttons (inventory.buttons > 0)
  → generate one action-based scenario per distinct, non-nav button
    (up to 3). Assert the CONSEQUENCE of the click (a dialog,
    heading, toast, new section) — never re-assert the button that
    was clicked.
- checkboxes / radios / selects (inventory.choices > 0)
  → generate a validation scenario that toggles the control and
    asserts a dependent element becomes enabled/visible.
- data table / list (inventory.data > 0)
  → generate a scenario that opens a row or sorts/filters the table
    and asserts the row detail or updated list is visible.

THEN-STEP QUALITY (very important)
----------------------------------
Every `Then` step must describe a CONSEQUENCE, not the action target.
Bad (re-asserts the thing just clicked):
  When User clicks the "Open Stewie assistant" button
  Then User sees the "Open Stewie assistant" interface   ← avoid

Good (asserts the resulting UI state):
  When User clicks the "Open Stewie assistant" button
  Then The Stewie chat panel is displayed
  Then The Ask Stewie message box is visible

Prefer Then-step subjects that name a DIFFERENT element id / heading
than the When-step subject. Reference headings from the provided
`headings` list when naming post-action screens.

TIER SELECTION
--------------
- smoke: 1-2 scenarios covering the primary action of the page.
- happy: 1-2 scenarios covering the main user flow end-to-end.
- validation: 1+ scenarios for each form present (empty / invalid input).
- navigation: 1 scenario when nav links are present.
- edge: optional — only when the page clearly has edge conditions
  (pagination controls, max-length inputs, disabled-button states).
Only generate scenarios for tiers listed in `tiers`. If a tier has
no meaningful coverage on this page, skip it rather than padding.
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


_SEARCH_HINTS = ("search", "find", "lookup", "query", "filter")
_CHAT_HINTS = ("ask", "chat", "message", "question", "prompt", "stewie")
_NAV_HINTS = ("home", "back", "menu", "nav", "sidebar", "tab", "dashboard")
_DATA_HINTS = ("row", "cell", "table", "list", "grid", "pagination", "page")
_SUBMIT_HINTS = ("submit", "send", "save", "apply", "confirm", "continue", "next")


def _has_hint(element: Element, hints: tuple[str, ...]) -> bool:
    haystack = " ".join(
        str(x) for x in (element.id, element.name or "", element.role or "")
    ).lower()
    return any(h in haystack for h in hints)


def build_ui_inventory(extraction: PageExtraction) -> dict:
    """Summarise the page's automation surface for the planner.

    The inventory counts each class of interactive component. The
    feature-plan LLM uses these counts to decide *which test types*
    are warranted for this specific page, instead of emitting a
    generic ``smoke + happy + validation`` shape that happens to
    ignore the real UI.
    """
    search: list[str] = []
    chat: list[str] = []
    nav: list[str] = []
    data_controls: list[str] = []
    action_buttons: list[str] = []
    choices: list[str] = []
    submits: list[str] = []

    for e in extraction.elements:
        if e.kind in ("input", "textarea"):
            if _has_hint(e, _CHAT_HINTS):
                chat.append(e.id)
            elif _has_hint(e, _SEARCH_HINTS):
                search.append(e.id)
        if e.kind == "link" or e.role == "tab" or _has_hint(e, _NAV_HINTS):
            if e.kind in ("link", "tab", "menuitem") or e.role in ("link", "tab", "menuitem"):
                nav.append(e.id)
        if e.kind in ("row", "cell"):
            data_controls.append(e.id)
        if e.kind == "button":
            if _has_hint(e, _SUBMIT_HINTS):
                submits.append(e.id)
            elif _has_hint(e, _NAV_HINTS):
                # nav-shaped buttons already in `nav` (via _NAV_HINTS);
                # avoid double-counting as action.
                if e.id not in nav:
                    nav.append(e.id)
            else:
                action_buttons.append(e.id)
        if e.kind in ("checkbox", "radio", "select"):
            choices.append(e.id)

    return {
        "search": search[:5],
        "chat": chat[:5],
        "nav": nav[:8],
        "data": data_controls[:5],
        "buttons": action_buttons[:8],
        "choices": choices[:6],
        "submits": submits[:4],
        "forms": len(extraction.forms),
        "headings": extraction.headings[:8],
    }


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
    inventory = build_ui_inventory(extraction)
    payload = {
        "url": extraction.final_url,
        "title": extraction.title,
        "headings": extraction.headings,
        "pom_methods": pom_method_names,
        "elements": _compact_elements(extraction.elements),
        "tiers": requested_tiers,
        "ui_inventory": inventory,
    }
    return (
        "Build a feature plan for this page. Use `ui_inventory` to pick "
        "test types (search → search tests, chat → chat tests, forms → "
        "submit + validation, nav → navigation, buttons → action with "
        "CONSEQUENCE assertions). Every Then step must name a DIFFERENT "
        "element than its When step. Reuse existing pom_methods where "
        "possible; set pom_method=null for pure assertions.\n\n"
        + json.dumps(payload, separators=(",", ":"))
    )
