"""Base class shared by every generated POM.

Provides the self-healing locator helper (:meth:`locate`) plus
action-level self-heal wrappers (:meth:`click`, :meth:`check`,
:meth:`fill`, :meth:`select`) that generated POMs call instead of
raw Playwright methods.

Two capabilities layered on top of the raw Playwright Locator:

* **Consent-checkbox unblock** — when a click/check/fill target is
  disabled, look for visible unchecked checkboxes on the page
  (native and ARIA ``role=checkbox``) and try ticking them one at a
  time. After each tick, re-probe the target; if it becomes enabled
  we proceed.
* **Diagnostic timeout rewrap** — when a Playwright action times out,
  the raw error is an opaque "Timeout 30000ms exceeded". We catch it
  and re-raise an :class:`AssertionError` that names *why* the action
  never landed (element missing from DOM / ambiguous selector /
  hidden / disabled / detached / genuinely timed out). The new
  message is what the failing test's traceback shows and what the
  heal layer's ``failure_class`` classifier reads.

The heal layer is **opt-in per call** via the ``heal`` argument
(defaults to ``True``). Pass ``heal=False`` to explicitly assert a
disabled state, e.g. in a negative scenario that tests "submit stays
disabled without consent".
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

        On timeout, re-raises with a diagnostic message explaining
        *why* the click never landed instead of an opaque
        ``TimeoutError``.
        """
        locator = self.locate(element_id)
        if heal and not self._is_enabled(locator):
            if self._unblock_via_consent(locator):
                pass  # fall through to normal click
        try:
            locator.click(timeout=timeout_ms)
        except PWTimeout as exc:
            self._diagnose(element_id, "click", locator, exc)

    def check(self, element_id: str, *, heal: bool = True, timeout_ms: int = 10_000) -> None:
        """Check ``element_id``. Idempotent — does nothing if already checked."""
        locator = self.locate(element_id)
        if heal:
            try:
                if locator.is_checked():
                    return
            except Exception:
                pass
        try:
            locator.check(timeout=timeout_ms)
        except PWTimeout as exc:
            self._diagnose(element_id, "check", locator, exc)

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
        try:
            locator.fill(value, timeout=timeout_ms)
        except PWTimeout as exc:
            self._diagnose(element_id, "fill", locator, exc)

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
        try:
            locator.select_option(value, timeout=timeout_ms)
        except PWTimeout as exc:
            self._diagnose(element_id, "select", locator, exc)

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

    # ------------------------------------------------------------------
    # Diagnostic rewrap — turns a raw Playwright timeout into an
    # actionable AssertionError. Order of probes matters: we start
    # with cheap, most-specific checks and widen outward.
    # ------------------------------------------------------------------

    def _diagnose(
        self,
        element_id: str,
        action: str,
        locator: Locator,
        exc: Exception,
    ) -> None:
        """Raise an :class:`AssertionError` explaining why ``action`` failed.

        The wrapped Playwright error is preserved in ``__cause__`` so
        the raw traceback is still available in ``pytest -vv`` output.
        The failure-class classifier in
        :mod:`autocoder.heal.pytest_failures` reads the phrases
        emitted here (``element is not enabled`` etc.) to route the
        failure into the right bucket, so changes to the phrasing
        below should preserve those tokens.
        """
        try:
            count = locator.count()
        except Exception:
            count = -1
        try:
            visible = bool(locator.is_visible())
        except Exception:
            visible = False
        try:
            enabled = bool(locator.is_enabled())
        except Exception:
            enabled = True
        try:
            box = locator.bounding_box()
        except Exception:
            box = None

        if count == 0:
            msg = (
                f"[{action} {element_id!r}] NO MATCH — selector resolved to 0 "
                f"elements. Page changed since extraction; regenerate the POM "
                f"or let the heal stage repair the selector. "
                f"(no selector resolved)"
            )
        elif count > 1:
            msg = (
                f"[{action} {element_id!r}] AMBIGUOUS — {count} elements match "
                f"the locator. Selector is too loose; tighten it or pick a "
                f"`.nth()`."
            )
        elif not visible:
            msg = (
                f"[{action} {element_id!r}] HIDDEN — element is in the DOM but "
                f"not visible (display:none, offscreen, or zero-size). "
                f"(element is not visible)"
            )
        elif not enabled:
            msg = (
                f"[{action} {element_id!r}] DISABLED — element is visible but "
                f"not interactive. Check whether a prerequisite step is "
                f"missing (consent checkbox, form validation, loading spinner). "
                f"(element is not enabled)"
            )
        elif box is None:
            msg = (
                f"[{action} {element_id!r}] DETACHED — element is in the DOM "
                f"tree but has no bounding box. The SPA likely removed it "
                f"between resolve + action. (element is not attached)"
            )
        else:
            msg = (
                f"[{action} {element_id!r}] TIMEOUT — element looks actionable "
                f"but the operation never resolved. Usually a network-bound "
                f"handler or an animation delaying the click target. "
                f"(Timeout {action} exceeded)"
            )
        raise AssertionError(msg) from exc
