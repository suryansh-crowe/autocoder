"""Stable selector resolution.

A stable selector is the *cheapest unambiguous* locator we can find for
a given DOM element. Cheapest means: shortest to express, most likely
to survive UI churn. The priority order matches the spec the user
defined:

1. test ids (``data-testid``, ``data-test``, ``data-cy``, ``data-qa``)
2. semantic role + accessible name (``getByRole``)
3. label (``getByLabel``)
4. placeholder (``getByPlaceholder``)
5. anchor text (``getByText``)
6. CSS / XPath (last resort, scoped)

Each :func:`build_selector` call returns a primary selector plus a
short list of fallbacks. The runtime self-healing layer
(:mod:`tests.support.locator_strategy`) walks the fallback list when
the primary fails, so a single brittle ``id`` change does not break the
test.
"""

from __future__ import annotations

from typing import Any

from playwright.sync_api import ElementHandle, Locator, Page

from autocoder import logger
from autocoder.models import SelectorStrategy, StableSelector


_TEST_ID_ATTRS = ("data-testid", "data-test-id", "data-test", "data-cy", "data-qa", "data-automation-id")


def _coalesce(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip()
        return v or None
    return None


def _attr(handle: ElementHandle, name: str) -> str | None:
    try:
        return _coalesce(handle.get_attribute(name))
    except Exception:
        return None


def _role(handle: ElementHandle) -> str | None:
    role = _attr(handle, "role")
    if role:
        return role
    tag = (handle.evaluate("el => el.tagName") or "").lower()
    return {
        "button": "button",
        "a": "link",
        "input": "textbox",
        "textarea": "textbox",
        "select": "combobox",
        "h1": "heading",
        "h2": "heading",
        "h3": "heading",
        "h4": "heading",
        "img": "image",
    }.get(tag)


def _accessible_name(handle: ElementHandle) -> str | None:
    for source in ("aria-label", "alt", "title"):
        name = _attr(handle, source)
        if name:
            return name
    text = _coalesce(handle.evaluate("el => el.innerText || el.textContent"))
    if text:
        # accessible name is single-line; collapse whitespace
        return " ".join(text.split())[:80] or None
    value = _attr(handle, "value")
    return value


def _css_id(handle: ElementHandle) -> str | None:
    el_id = _attr(handle, "id")
    if not el_id:
        return None
    # Skip framework-generated ids (they shift between renders)
    if any(token in el_id for token in (":r", "__react", "mui-", "chakra-")):
        return None
    return f"#{el_id}"


def _name_attr(handle: ElementHandle) -> str | None:
    return _attr(handle, "name")


def _label_text(handle: ElementHandle) -> str | None:
    """Best-effort: associated <label for=id> or wrapping label."""
    el_id = _attr(handle, "id")
    page = handle.owner_frame().page if hasattr(handle, "owner_frame") else None
    if el_id and page is not None:
        try:
            label = page.locator(f"label[for={el_id!r}]").first
            text = _coalesce(label.text_content(timeout=500))
            if text:
                return " ".join(text.split())[:80]
        except Exception:
            pass
    aria_label = _attr(handle, "aria-label")
    if aria_label:
        return aria_label
    return None


def _placeholder(handle: ElementHandle) -> str | None:
    return _attr(handle, "placeholder")


def build_selector(handle: ElementHandle) -> tuple[StableSelector, list[StableSelector]]:
    """Return ``(primary, fallbacks)`` for a Playwright ElementHandle."""
    candidates: list[StableSelector] = []

    role = _role(handle)
    name = _accessible_name(handle)

    for attr in _TEST_ID_ATTRS:
        val = _attr(handle, attr)
        if val:
            candidates.append(
                StableSelector(strategy=SelectorStrategy.TEST_ID, value=val, role=role, name=name)
            )
            break

    if role and name:
        candidates.append(
            StableSelector(strategy=SelectorStrategy.ROLE_NAME, value=role, role=role, name=name)
        )

    label = _label_text(handle)
    if label:
        candidates.append(
            StableSelector(strategy=SelectorStrategy.LABEL, value=label, role=role, name=name)
        )

    placeholder = _placeholder(handle)
    if placeholder:
        candidates.append(
            StableSelector(strategy=SelectorStrategy.PLACEHOLDER, value=placeholder, role=role)
        )

    if name and role in {"button", "link", "tab", "menuitem", "heading"}:
        candidates.append(
            StableSelector(strategy=SelectorStrategy.TEXT, value=name, role=role)
        )

    css_id = _css_id(handle)
    if css_id:
        candidates.append(
            StableSelector(strategy=SelectorStrategy.CSS, value=css_id, role=role, name=name)
        )

    name_attr = _name_attr(handle)
    if name_attr:
        candidates.append(
            StableSelector(
                strategy=SelectorStrategy.CSS,
                value=f"[name={name_attr!r}]",
                role=role,
                name=name,
            )
        )

    if not candidates:
        # Absolute last resort — XPath of the element. Self-healing
        # layer will report it as fragile so the user is aware.
        try:
            xpath = handle.evaluate(
                "el => { let p = ''; while (el && el.nodeType === 1) {"
                "  let i = 1, s = el.previousSibling;"
                "  while (s) { if (s.nodeType === 1 && s.nodeName === el.nodeName) i++; s = s.previousSibling; }"
                "  p = '/' + el.nodeName.toLowerCase() + '[' + i + ']' + p; el = el.parentNode;"
                "} return p; }"
            )
        except Exception:
            xpath = "//*"
        candidates.append(StableSelector(strategy=SelectorStrategy.XPATH, value=xpath, role=role, name=name))

    primary = candidates[0]
    fallbacks = [c for c in candidates[1:] if c != primary][:4]
    logger.debug(
        "selector_picked",
        primary=primary.strategy.value,
        primary_value=primary.value[:60],
        role=primary.role or "",
        name=(primary.name or "")[:40],
        fallbacks=",".join(f.strategy.value for f in fallbacks) or "-",
    )
    if primary.strategy in {SelectorStrategy.CSS, SelectorStrategy.XPATH}:
        logger.debug(
            "selector_fragile",
            strategy=primary.strategy.value,
            reason="no test_id / role+name / label / placeholder available",
            value=primary.value[:80],
        )
    return primary, fallbacks


def build_selector_from_locator(page: Page, locator: Locator) -> tuple[StableSelector, list[StableSelector]] | None:
    handle = locator.element_handle()
    if handle is None:
        return None
    return build_selector(handle)


def to_playwright_call(selector: StableSelector) -> str:
    """Serialise a selector into a Playwright call (sync API).

    Used by the renderer when emitting POM methods.
    """
    if selector.strategy == SelectorStrategy.TEST_ID:
        return f"page.get_by_test_id({selector.value!r})"
    if selector.strategy == SelectorStrategy.ROLE_NAME:
        if selector.name:
            return f"page.get_by_role({selector.value!r}, name={selector.name!r})"
        return f"page.get_by_role({selector.value!r})"
    if selector.strategy == SelectorStrategy.LABEL:
        return f"page.get_by_label({selector.value!r})"
    if selector.strategy == SelectorStrategy.PLACEHOLDER:
        return f"page.get_by_placeholder({selector.value!r})"
    if selector.strategy == SelectorStrategy.TEXT:
        return f"page.get_by_text({selector.value!r}, exact=False)"
    if selector.strategy == SelectorStrategy.CSS:
        return f"page.locator({selector.value!r})"
    if selector.strategy == SelectorStrategy.XPATH:
        return f"page.locator('xpath={selector.value}')"
    raise ValueError(f"Unknown strategy: {selector.strategy!r}")
