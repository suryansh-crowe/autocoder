"""Page inspector — produce a compact PageExtraction.

We deliberately *do not* dump the full DOM or accessibility tree. The
LLM only sees the elements that actually matter for automation, with
the strongest selector we could find for each one. That is the largest
single lever for keeping prompt token counts low.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from playwright.sync_api import ElementHandle, Page

from autocoder import logger
from autocoder.config import Settings
from autocoder.extract.selectors import build_selector
from autocoder.models import (
    Element,
    FormSpec,
    PageExtraction,
    URLKind,
)
from autocoder.utils import fingerprint, python_identifier


_INTERACTIVE_SELECTOR = (
    "button, a[href], input:not([type=hidden]), textarea, select, "
    "[role=button], [role=link], [role=tab], [role=menuitem], [role=checkbox], "
    "[role=radio], [role=switch], [role=combobox], [role=textbox], "
    "[contenteditable=true]"
)


def _kind_for(role: str | None, tag: str | None) -> str:
    """Map (role, tag) to the catalog `kind` used by the planner.

    Role wins over tag — that's how the LLM-facing label stays
    consistent for `<input type=checkbox>` (role=checkbox via the
    selectors module) vs a generic text `<input>` (role=textbox).
    """
    role = (role or "").lower()
    tag = (tag or "").lower()
    if role == "checkbox":
        return "checkbox"
    if role == "radio":
        return "radio"
    if role == "button" or tag == "button":
        return "button"
    if role == "link" or tag == "a":
        return "link"
    if role == "tab":
        return "tab"
    if role == "menuitem":
        return "menuitem"
    if tag == "input":
        return "input"
    if tag == "textarea":
        return "textarea"
    if tag == "select" or role == "combobox":
        return "select"
    return "other"


def _short(text: str | None, limit: int = 60) -> str | None:
    if not text:
        return None
    cleaned = " ".join(text.split())
    return cleaned[:limit] if cleaned else None


def _element_id(seed: str, used: Counter[str]) -> str:
    base = python_identifier(seed) or "el"
    used[base] += 1
    if used[base] == 1:
        return base
    return f"{base}_{used[base]}"


def _collect_headings(page: Page, limit: int = 8) -> list[str]:
    out: list[str] = []
    try:
        handles = page.query_selector_all("h1, h2, h3, [role=heading]")
    except Exception:
        return out
    for h in handles[:limit]:
        text = _short(h.text_content(), 80)
        if text:
            out.append(text)
    return out


def _collect_forms(page: Page, element_lookup: dict[str, str]) -> list[FormSpec]:
    forms: list[FormSpec] = []
    try:
        form_handles = page.query_selector_all("form")
    except Exception:
        return forms
    for idx, form in enumerate(form_handles):
        form_id = form.get_attribute("id") or form.get_attribute("name") or f"form_{idx + 1}"
        form_id = python_identifier(form_id)
        fields: list[str] = []
        submit_id: str | None = None
        for child in form.query_selector_all("input, textarea, select, button[type=submit], [role=button]") or []:
            child_handle = child.evaluate("el => el.outerHTML.slice(0, 60)")
            key = element_lookup.get(child_handle)
            if key:
                fields.append(key)
                tag = (child.evaluate("el => el.tagName") or "").lower()
                input_type = (child.get_attribute("type") or "").lower()
                if (tag == "button" and input_type == "submit") or (tag == "input" and input_type == "submit"):
                    submit_id = key
        forms.append(FormSpec(id=form_id, fields=fields, submit_id=submit_id))
    return forms


def _enumerate_interactive(page: Page, max_elements: int) -> list[ElementHandle]:
    try:
        handles = page.query_selector_all(_INTERACTIVE_SELECTOR)
    except Exception:
        return []
    visible: list[ElementHandle] = []
    for h in handles:
        try:
            if not h.is_visible():
                continue
        except Exception:
            continue
        visible.append(h)
        if len(visible) >= max_elements:
            break
    return visible


def extract_page(
    page: Page,
    *,
    url: str,
    settings: Settings,
    requires_auth: bool = False,
    kind: URLKind = URLKind.UNKNOWN,
) -> PageExtraction:
    """Build a compact :class:`PageExtraction` for the URL the page is on."""
    elements: list[Element] = []
    element_lookup: dict[str, str] = {}
    used_ids: Counter[str] = Counter()

    handles = _enumerate_interactive(page, settings.extraction.max_elements_per_page)
    logger.debug(
        "inspector_enumerated",
        url=logger.safe_url(url),
        candidates=len(handles),
        cap=settings.extraction.max_elements_per_page,
        capped=len(handles) >= settings.extraction.max_elements_per_page,
    )
    skipped = 0
    for handle in handles:
        try:
            primary, fallbacks = build_selector(handle)
        except Exception as exc:  # noqa: BLE001
            skipped += 1
            logger.debug("inspector_skip_element", err=str(exc)[:80])
            continue

        tag = (handle.evaluate("el => el.tagName") or "").lower()
        kind_str = _kind_for(primary.role, tag)

        seed = primary.name or primary.value
        if not seed:
            seed = f"{tag}_{len(elements) + 1}"
        eid = _element_id(seed, used_ids)

        try:
            enabled = handle.is_enabled()
        except Exception:
            enabled = True
        try:
            visible = handle.is_visible()
        except Exception:
            visible = True

        element = Element(
            id=eid,
            role=primary.role or kind_str,
            name=_short(primary.name, 80),
            kind=kind_str,
            selector=primary,
            fallbacks=fallbacks,
            visible=visible,
            enabled=enabled,
        )
        elements.append(element)

        try:
            sig = handle.evaluate("el => el.outerHTML.slice(0, 60)")
            element_lookup[sig] = eid
        except Exception:
            pass

    forms = _collect_forms(page, element_lookup)
    headings = _collect_headings(page)
    logger.debug(
        "inspector_summary",
        url=logger.safe_url(url),
        elements=len(elements),
        forms=len(forms),
        headings=len(headings),
        skipped=skipped,
    )

    payload: dict[str, Any] = {
        "elements": [e.model_dump(mode="json") for e in elements],
        "headings": headings,
        "forms": [f.model_dump(mode="json") for f in forms],
    }

    return PageExtraction(
        url=url,
        final_url=page.url,
        title=page.title() or "",
        kind=kind,
        requires_auth=requires_auth,
        redirected_to=page.url if page.url != url else None,
        elements=elements,
        forms=forms,
        headings=headings,
        fingerprint=fingerprint(payload),
    )
