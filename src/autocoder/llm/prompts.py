"""Prompt builders.

Three prompts are sent to the model, once per target page. All three
ask for *only* a JSON object:

* :func:`build_pom_prompt`     — page object plan              (prompt 1)
* :func:`build_feature_prompt` — per-control-type Gherkin       (prompt 2)
* :func:`build_steps_prompt`   — Playwright body per Gherkin step (prompt 3)

All three prompts contain just the compact element catalog
(id + role + name + kind) plus plan metadata — no DOM dumps, no full
a11y trees, no source code. That keeps the total input under ~1.5k
tokens for typical pages. ``known_pages`` is an optional list that
names the OTHER pages discovered in this run so the AI can reason
about cross-page navigation (e.g., "User clicks Catalog" is a nav
away from the current POM toward the ``catalog`` page).
"""

from __future__ import annotations

import json
from typing import Iterable

from autocoder.models import Element, PageExtraction
from autocoder.prompts import load_system


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

POM_SYSTEM = load_system('pom_plan')

FEATURE_SYSTEM = load_system('feature_plan')


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
_SUBMIT_HINTS = ("submit", "send", "save", "apply", "confirm", "continue")
_PAGINATION_HINTS = ("next", "previous", "prev", "page ", "pagination")


# POM method name fragments that identify a state-changing submit.
# Consumed by ``_submit_method_names`` to populate ``submit_methods``
# in the feature-plan payload — the prompt uses this list to enforce
# the PHANTOM SUBMIT ban. Keep in sync with the hint sets above.
_SUBMIT_METHOD_HINTS = (
    "submit", "send", "save", "apply", "confirm", "continue",
    "filter", "search", "ask", "next", "login", "signin",
)


def _submit_method_names(pom_methods: Iterable[str]) -> list[str]:
    """POM methods that count as a real submit trigger for a form/search.

    The feature-plan and steps-plan prompts need to know which
    pom_methods legitimately fire a form/search submission. Three
    shapes qualify:

    * ``submit_*`` — the canonical POM-level submit method. Emitted
      by the POM planner for search/chat/form inputs that lack an
      adjacent submit button (action=``press_enter``).
    * ``press_enter_*`` — legacy / explicit keyboard-submit naming.
    * ``click_*<submit-hint>*`` — click on a submit/filter/search
      button whose method name carries a submit keyword.
    """
    out: list[str] = []
    for name in pom_methods:
        if not name:
            continue
        lname = name.lower()
        if lname.startswith("submit_") or lname.startswith("press_enter"):
            out.append(name)
            continue
        if not lname.startswith("click_"):
            continue
        if any(h in lname for h in _SUBMIT_METHOD_HINTS):
            out.append(name)
    return out


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
    pagination: list[str] = []

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
            if _has_hint(e, _PAGINATION_HINTS):
                pagination.append(e.id)
            elif _has_hint(e, _SUBMIT_HINTS):
                submits.append(e.id)
            elif _has_hint(e, _NAV_HINTS):
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
        "pagination": pagination[:5],
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
    known_pages: list[dict] | None = None,
) -> str:
    inventory = build_ui_inventory(extraction)
    # ``preload_visible_ids`` is the set of elements that were already
    # on the page at extraction time. The feature-plan prompt uses it
    # to reject Then-steps that assert visibility on page-chrome that
    # existed before the action — those assertions pass trivially and
    # test nothing.
    preload_visible_ids = [e.id for e in extraction.elements if e.visible]
    # ``submit_methods`` is the subset of POM methods that count as a
    # real form/search submit trigger. The prompt uses this to reject
    # PHANTOM SUBMIT scenarios — attempts to "submit" when no submit
    # target exists.
    submit_methods = _submit_method_names(pom_method_names)
    payload = {
        "url": extraction.final_url,
        "title": extraction.title,
        "headings": extraction.headings,
        "pom_methods": pom_method_names,
        "elements": _compact_elements(extraction.elements),
        "tiers": requested_tiers,
        "ui_inventory": inventory,
        "known_pages": list(known_pages or []),
        "preload_visible_ids": preload_visible_ids,
        "submit_methods": submit_methods,
    }
    return (
        "Build a feature plan for this page. Use `ui_inventory` to pick "
        "test types (search → search tests, chat → chat tests, forms → "
        "submit + validation, nav → navigation, buttons → action with "
        "CONSEQUENCE assertions). Every Then step must name a DIFFERENT "
        "element than its When step. Reuse existing pom_methods where "
        "possible; set pom_method=null for pure assertions. When a link "
        "or button targets one of the `known_pages`, write the scenario "
        "as a CROSS-PAGE navigation — the Then step should reference the "
        "target page's URL or a landmark that only appears there. Obey "
        "the five hard bans in the system prompt (ZERO-ACTION, "
        "DUPLICATE ACTION, PHANTOM SUBMIT, PREEXISTING-ELEMENT "
        "ASSERTION, RE-ASSERT WHEN TARGET) — use `preload_visible_ids` "
        "and `submit_methods` to self-check before emitting.\n\n"
        + json.dumps(payload, separators=(",", ":"))
    )


# ---------------------------------------------------------------------------
# Prompt 3 — Playwright step bodies (one per unique Gherkin step)
# ---------------------------------------------------------------------------


STEPS_SYSTEM = load_system('steps_plan')


def build_steps_prompt(
    extraction: PageExtraction,
    *,
    feature_plan_scenarios: list[dict],
    feature_plan_background: list[dict],
    pom_class: str,
    pom_fixture: str,
    pom_methods: list[dict],
    known_pages: list[dict] | None = None,
) -> str:
    """Payload for :data:`STEPS_SYSTEM`.

    Carries the feature plan's step texts (grouped by scenario), the
    POM method catalog, the compact element catalog, and a compact
    ``known_pages`` snapshot so the AI can bind cross-page nav steps
    to URL-level assertions.

    Also surfaces three validator-aligned sets so the binder can
    self-avoid the same bans the steps validator enforces
    downstream:

    * ``preload_visible_ids`` — element ids visible at page load.
      Rule 2 (PREEXISTING-ELEMENT VISIBILITY) forbids
      ``to_be_visible`` assertions on these ids outside cross-page
      nav scenarios.
    * ``submit_methods`` — POM methods that count as real submit
      triggers. Rule 3 (PHANTOM SUBMIT) forces submit-shaped steps
      to bind to one of these, not to a re-fill.
    * ``cross_page_nav_methods`` — POM methods whose click lands on
      a ``known_pages`` sibling. Rule 2 uses this set to decide
      whether a Then step was preceded by a cross-page navigation.
    """
    # Local import: :mod:`generate.steps` is a sibling package; importing
    # at module-top would pull the generate package in during LLM-only
    # code paths. Lazy-import keeps the dependency narrow.
    from autocoder.generate.steps import nav_method_names

    preload_visible_ids = [e.id for e in extraction.elements if e.visible]
    pom_method_names = [str(m.get("name") or "") for m in pom_methods if m.get("name")]
    submit_methods = _submit_method_names(pom_method_names)
    cross_page_nav_methods = sorted(
        nav_method_names(
            (
                (str(m.get("name") or ""), m.get("element_id"))
                for m in pom_methods
                if m.get("name")
            ),
            known_pages,
        )
    )

    payload = {
        "page_url": extraction.final_url,
        "pom_class": pom_class,
        "pom_fixture": pom_fixture,
        "pom_methods": pom_methods,
        "elements": _compact_elements(extraction.elements),
        "known_pages": list(known_pages or []),
        "preload_visible_ids": preload_visible_ids,
        "submit_methods": submit_methods,
        "cross_page_nav_methods": cross_page_nav_methods,
        "feature_plan": {
            "background": feature_plan_background,
            "scenarios": feature_plan_scenarios,
        },
    }
    return (
        "Implement each unique Gherkin step as ONE Python statement. "
        "Start scenarios with `<fixture>.navigate()` via the background. "
        "Never re-assert the element that was just clicked/filled. "
        "For cross-page jumps, assert the URL instead of re-asserting "
        "the source page. Obey the five hard bans in the system "
        "prompt (DUPLICATE-ACTION BODY, PREEXISTING-ELEMENT "
        "VISIBILITY, PHANTOM SUBMIT, RE-ASSERT WHEN TARGET, "
        "METHOD-STEP TOKEN MISMATCH) — use `preload_visible_ids`, "
        "`submit_methods`, and `cross_page_nav_methods` to self-check "
        "before emitting each binding.\n\n"
        + json.dumps(payload, separators=(",", ":"))
    )
