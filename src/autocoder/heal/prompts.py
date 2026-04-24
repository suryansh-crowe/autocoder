"""Prompts for the heal stage.

Two prompt builders share the same JSON-only output contract:

* :func:`build_heal_prompt` — for un-implemented stubs the renderer
  left behind. Tiny envelope, single Python statement out.
* :func:`build_failure_heal_prompt` — for steps that *did* run but
  failed at runtime. Same schema, but the envelope carries the
  current body + the Playwright error so the model can reason
  about prerequisites (disabled buttons, modals, wrong primitive).

The *system* prompts themselves live as JSON files under
``src/autocoder/prompts/`` (``heal_stub.json`` +
``heal_failure.json``) and are loaded at import via
:func:`autocoder.prompts.load_system`, so they can be tweaked without
editing Python.

Both builders take care to TRIM the user-prompt payload — long
pom_method names (e.g. chat-suggestion buttons on the Stewie page
often compile to methods like ``who_is_the_data_owner_and_data_``
``steward_for_account_type_table``) balloon the payload and
occasionally trip Azure OpenAI's content-management policy. The
trim here keeps only the fields the heal system prompts actually
read and reduces each method entry to ``name`` + ``element_id``,
which is enough context for heal without feeding the full intent /
action / args strings into every prompt.
"""

from __future__ import annotations

import json
from typing import Iterable

from autocoder.prompts import load_system


HEAL_SYSTEM = load_system('heal_stub')


def _trim_pom_methods(pom_methods: list[dict]) -> list[dict]:
    """Strip pom_method dicts down to the two fields heal consults.

    ``name`` for binding, ``element_id`` for locate-fallbacks. Dropping
    ``intent``, ``action``, ``args`` shrinks the payload by roughly
    50-70% on pages with long chat-suggestion methods without losing
    any information heal actually uses.
    """
    out: list[dict] = []
    for m in pom_methods:
        name = (m.get("name") or "").strip()
        if not name:
            continue
        entry: dict = {"name": name}
        eid = (m.get("element_id") or "").strip()
        if eid:
            entry["element_id"] = eid
        out.append(entry)
    return out


def _trim_elements(elements: list[dict]) -> list[dict]:
    """Strip element dicts to the fields heal actually uses.

    Keeps ``id``, ``kind``, optional ``name`` — drops ``visible``
    (already baked into preload_visible_ids) and any other extras.
    """
    out: list[dict] = []
    for e in elements:
        eid = (e.get("id") or "").strip()
        if not eid:
            continue
        entry: dict = {"id": eid, "kind": e.get("kind") or ""}
        name = (e.get("name") or "").strip()
        if name:
            entry["name"] = name
        out.append(entry)
    return out


def _preload_visible_names(
    elements: list[dict], preload_visible_ids: list[str]
) -> list[str]:
    """Names of elements visible at page load, used by the weak-text rule.

    The heal prompts use this list to refuse ``get_by_text('<literal>')``
    and ``get_by_role('<role>', name='<literal>')`` bindings where the
    literal is a substring of any name here. Those fallbacks match the
    pre-existing UI and pass trivially — heal should emit a
    NotImplementedError stub instead.

    Names shorter than ``_MIN_NAME_LEN`` are excluded: a 1-2 character
    name (pagination button "2", icon-button "X") is too short to
    reliably indicate a substring collision. For example, the catalog
    page has pagination buttons named "1" / "2" / "192"; without the
    length filter, a legit Then like ``get_by_text('Page 2')`` would
    be rejected because "2" is a substring of "page 2" — but that
    "Page 2" text is genuine pagination-indicator content, not the
    pagination button's name.
    """
    id_set = set(preload_visible_ids)
    out: list[str] = []
    for e in elements:
        if e.get("id") not in id_set:
            continue
        name = (e.get("name") or "").strip()
        if name and len(name) >= _MIN_NAME_LEN:
            out.append(name)
    return out


# Minimum visible-name length that the weak-text rule considers. Names
# shorter than this are considered too noisy (risk of substring
# collision with legit data assertions like pagination indicators,
# numeric ids, or short column headers).
_MIN_NAME_LEN = 3


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
    # Lazy import: keeps the heal pipeline importable in pytest-only
    # contexts without pulling the llm package eagerly.
    from autocoder.llm.prompts import _submit_method_names

    preload_visible_ids = [
        e.get("id", "") for e in elements if e.get("visible", True) and e.get("id")
    ]
    preload_visible_names = _preload_visible_names(elements, preload_visible_ids)
    submit_methods = _submit_method_names(
        m.get("name", "") for m in pom_methods if m.get("name")
    )
    payload = {
        "step_text": step_text,
        "keywords": list(keywords),
        "pom_class": pom_class,
        "pom_fixture": fixture_name,
        "page_url": page_url or "",
        "forbidden_element_ids": list(forbidden_element_ids),
        "pom_methods": _trim_pom_methods(pom_methods),
        "elements": _trim_elements(elements),
        "preload_visible_ids": preload_visible_ids,
        "preload_visible_names": preload_visible_names,
        "submit_methods": submit_methods,
    }
    return (
        "Write the body for this Gherkin step. Follow the four "
        "constraints in the system prompt (CONSEQUENCE-NOT-TARGET "
        "via `forbidden_element_ids`, MISSING-SUBMIT via "
        "`submit_methods`, PREEXISTING-ELEMENT VISIBILITY via "
        "`preload_visible_ids`, WEAK-TEXT-FALLBACK via "
        "`preload_visible_names`) — self-check before emitting.\n\n"
        + json.dumps(payload, separators=(",", ":"))
    )


# ---------------------------------------------------------------------------
# Failure-driven heal
# ---------------------------------------------------------------------------


FAILURE_HEAL_SYSTEM = load_system('heal_failure')


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
    # Local import: avoids pulling the heavier ``llm`` package at module
    # load when the heal pipeline is imported in pytest-only contexts.
    from autocoder.llm.prompts import _submit_method_names

    preload_visible_ids = [
        e.get("id", "") for e in elements if e.get("visible", True) and e.get("id")
    ]
    preload_visible_names = _preload_visible_names(elements, preload_visible_ids)
    submit_methods = _submit_method_names(
        m.get("name", "") for m in pom_methods if m.get("name")
    )
    # Truncate verbose error messages — Playwright stack traces can be
    # 1-2KB of repeating context that the heal model does not use and
    # that occasionally trips the content filter.
    err_msg = (error_message or "")[:400]
    payload = {
        "step_text": step_text,
        "current_body": current_body,
        "error_message": err_msg,
        "failure_class": failure_class,
        "keywords": list(keywords),
        "pom_class": pom_class,
        "pom_fixture": fixture_name,
        "page_url": page_url or "",
        "pom_methods": _trim_pom_methods(pom_methods),
        "elements": _trim_elements(elements),
        "preload_visible_ids": preload_visible_ids,
        "preload_visible_names": preload_visible_names,
        "submit_methods": submit_methods,
    }
    return (
        "Suggest a revised body for this failing step. Follow the three "
        "constraints in the system prompt (MISSING-SUBMIT, "
        "PREEXISTING-ELEMENT VISIBILITY, WEAK-TEXT-FALLBACK) — use "
        "`preload_visible_ids`, `preload_visible_names`, and "
        "`submit_methods` to self-check before emitting.\n\n"
        + json.dumps(payload, separators=(",", ":"))
    )
