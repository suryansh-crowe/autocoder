"""Runtime self-healing locator resolver.

Each generated POM holds a ``SELECTORS`` dict::

    SELECTORS = {
        "submit": [
            {"strategy": "test_id", "value": "submit-btn"},
            {"strategy": "role_name", "value": "button", "name": "Submit"},
            {"strategy": "css", "value": "form button[type=submit]"},
        ],
    }

The resolver walks the list in order and returns the first selector
that yields a visible locator. If every selector fails it raises a
``LocatorNotFound`` with the full diagnostic trail so the failing
test is easy to debug.

This is the *runtime* counterpart to
:mod:`autocoder.extract.selectors` (which is the *generation-time*
counterpart). They share the same priority order; keep them in sync
when adding a new strategy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from playwright.sync_api import Locator, Page


SelectorSpec = dict


class LocatorNotFound(RuntimeError):
    pass


@dataclass
class _Attempt:
    strategy: str
    value: str
    error: str | None = None


def _build_locator(page: Page, spec: SelectorSpec) -> Locator:
    strategy = spec.get("strategy")
    value = spec["value"]
    if strategy == "test_id":
        return page.get_by_test_id(value)
    if strategy == "role_name":
        name = spec.get("name")
        if name:
            return page.get_by_role(value, name=name)
        return page.get_by_role(value)
    if strategy == "label":
        return page.get_by_label(value)
    if strategy == "placeholder":
        return page.get_by_placeholder(value)
    if strategy == "text":
        return page.get_by_text(value, exact=False)
    if strategy == "css":
        return page.locator(value)
    if strategy == "xpath":
        return page.locator(f"xpath={value}")
    raise ValueError(f"Unknown selector strategy: {strategy!r}")


def resolve(page: Page, specs: Iterable[SelectorSpec], *, timeout_ms: int = 4000) -> Locator:
    attempts: list[_Attempt] = []
    for spec in specs:
        try:
            locator = _build_locator(page, spec)
        except Exception as exc:
            attempts.append(_Attempt(spec.get("strategy", "?"), spec.get("value", "?"), str(exc)))
            continue
        try:
            locator.first.wait_for(state="attached", timeout=timeout_ms)
            return locator.first
        except Exception as exc:  # noqa: BLE001
            attempts.append(_Attempt(spec.get("strategy", "?"), spec.get("value", "?"), str(exc)))
    raise LocatorNotFound(
        "no selector resolved:\n  - " + "\n  - ".join(f"{a.strategy}={a.value!r} ({a.error})" for a in attempts)
    )
