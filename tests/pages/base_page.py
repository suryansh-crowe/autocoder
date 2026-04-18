"""Base class shared by every generated POM.

Provides one helper — :meth:`locate` — that wraps the runtime
self-healing resolver. Generated subclasses store their selector dict
as the ``SELECTORS`` class attribute and call ``self.locate('login_btn')``
inside each method.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from playwright.sync_api import Locator, Page

from tests.support.locator_strategy import SelectorSpec, resolve


class BasePage:
    SELECTORS: Mapping[str, Sequence[SelectorSpec]] = {}

    def __init__(self, page: Page, selectors: Mapping[str, Sequence[SelectorSpec]] | None = None) -> None:
        self.page = page
        if selectors is not None:
            self.SELECTORS = selectors

    def locate(self, element_id: str, *, timeout_ms: int = 4000) -> Locator:
        specs = self.SELECTORS.get(element_id)
        if not specs:
            raise KeyError(f"No selector definitions for element_id={element_id!r}")
        return resolve(self.page, specs, timeout_ms=timeout_ms)

    def goto(self, url: str) -> None:
        self.page.goto(url)
