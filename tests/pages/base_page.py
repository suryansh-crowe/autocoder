"""Base class shared by every generated POM.

Provides the self-healing locator helper (:meth:`locate`) plus
action-level self-heal wrappers (:meth:`click`, :meth:`check`,
:meth:`fill`, :meth:`select`) that generated POMs call instead of
raw Playwright methods.

Why action-level self-heal
--------------------------

Generated POMs are produced by a local LLM and populated from a
snapshot extraction. Both sources are fallible:

* The LLM may order scenario steps in a way that triggers a
  disabled-until-consent button before the consent control has been
  activated. Without self-heal, every such scenario fails with
  ``Locator.click: Timeout 30000ms exceeded. - element is not enabled``.
* A DOM captured during extraction can drift slightly by test time
  (a checkbox may default to unchecked instead of remembered-checked;
  an animation may still be running).

The wrappers below apply the same unblock tactic the orchestrator's
auth runner uses: when a click / check / fill target is disabled,
look for visible unchecked checkboxes on the page (native and ARIA
``role=checkbox``) and try ticking them one at a time. After each
tick, re-probe the target; if it becomes enabled we proceed.

The heal is **opt-in per call** via the ``heal`` argument (defaults
to ``True``). Pass ``heal=False`` to explicitly assert a disabled
state, e.g. in a negative scenario that tests "submit stays disabled
without consent".
"""

from __future__ import annotations

import contextlib
from typing import Mapping, Sequence

from playwright.sync_api import Locator, Page, TimeoutError as PWTimeout

from tests.support.locator_strategy import SelectorSpec, resolve


# How long we wait for a single click / check / fill to land before
# triggering the unblock heuristics.
_ACTION_TIMEOUT_MS = 4_000

# Maximum number of consent-checkbox candidates we will try to tick
# per action. Five is enough for every consent pattern we have seen
# (usually exactly one) without letting a broken page burn time.
_MAX_CONSENT_TICKS = 5


class BasePage:
    SELECTORS: Mapping[str, Sequence[SelectorSpec]] = {}

    def __init__(self, page: Page, selectors: Mapping[str, Sequence[SelectorSpec]] | None = None) -> None:
        self.page = page
        if selectors is not None:
            self.SELECTORS = selectors

    # ------------------------------------------------------------------
    # Locator resolution
    # ------------------------------------------------------------------

    def locate(self, element_id: str, *, timeout_ms: int = _ACTION_TIMEOUT_MS) -> Locator:
        specs = self.SELECTORS.get(element_id)
        if not specs:
            raise KeyError(f"No selector definitions for element_id={element_id!r}")
        return resolve(self.page, specs, timeout_ms=timeout_ms)

    def goto(self, url: str) -> None:
        self.page.goto(url)

    # ------------------------------------------------------------------
    # Self-healing action wrappers — generated POMs call these.
    # ------------------------------------------------------------------

    def click(self, element_id: str, *, heal: bool = True, timeout_ms: int = 30_000) -> None:
        """Click ``element_id`` with optional consent-checkbox unblock.

        When ``heal=True`` (default) and the element is disabled at
        click time, we tick visible unchecked consent checkboxes one
        by one and retry the click. When ``heal=False`` we call
        ``.click()`` directly — use this from negative scenarios that
        explicitly verify a disabled state.
        """
        locator = self.locate(element_id)
        if heal and not self._is_enabled(locator):
            if self._unblock_via_consent(locator):
                # pathing worked; fall through to normal click
                pass
        locator.click(timeout=timeout_ms)

    def check(self, element_id: str, *, heal: bool = True, timeout_ms: int = 10_000) -> None:
        """Check ``element_id``. Idempotent — does nothing if already checked."""
        locator = self.locate(element_id)
        if heal:
            try:
                if locator.is_checked():
                    return
            except Exception:
                pass
        locator.check(timeout=timeout_ms)

    def fill(
        self,
        element_id: str,
        value: str,
        *,
        heal: bool = True,
        timeout_ms: int = 10_000,
    ) -> None:
        """Fill ``element_id`` with ``value``. Clears the field first when ``heal``."""
        locator = self.locate(element_id)
        if heal:
            with contextlib.suppress(Exception):
                locator.fill("", timeout=2_000)
        locator.fill(value, timeout=timeout_ms)

    def select(
        self,
        element_id: str,
        value: str,
        *,
        heal: bool = True,
        timeout_ms: int = 10_000,
    ) -> None:
        locator = self.locate(element_id)
        if heal and not self._is_enabled(locator):
            self._unblock_via_consent(locator)
        locator.select_option(value, timeout=timeout_ms)

    # ------------------------------------------------------------------
    # Self-heal primitives
    # ------------------------------------------------------------------

    @staticmethod
    def _is_enabled(locator: Locator) -> bool:
        try:
            return bool(locator.is_enabled())
        except Exception:
            return False

    def _unblock_via_consent(self, target: Locator) -> bool:
        """Tick visible consent checkboxes until ``target`` is enabled.

        Returns True if the target became enabled (either already was
        or the heal succeeded). Checkboxes are tried in two passes:

        1. ``input[type=checkbox]:not(:checked)`` — native controls.
        2. ``[role=checkbox][aria-checked="false"]`` — ARIA wrappers
           (React-MUI / Chakra / Radix ship with these).
        """
        if self._is_enabled(target):
            return True

        ticked = 0
        for sel in (
            'input[type="checkbox"]:not(:checked)',
            '[role="checkbox"][aria-checked="false"]',
        ):
            try:
                candidates = self.page.locator(sel)
                count = min(candidates.count(), _MAX_CONSENT_TICKS)
            except Exception:
                continue
            for i in range(count):
                cb = candidates.nth(i)
                try:
                    if not cb.is_visible():
                        continue
                    try:
                        cb.check(timeout=2_000)
                    except Exception:
                        # Some consent controls only respond to click
                        # on the parent label, so fall back.
                        try:
                            cb.click(timeout=2_000)
                        except Exception:
                            continue
                    ticked += 1
                except Exception:
                    continue
                if self._is_enabled(target):
                    return True
        return self._is_enabled(target)
