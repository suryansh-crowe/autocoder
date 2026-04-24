"""Interactive element expansion for the extractor.

The first-pass DOM scrape in :func:`autocoder.extract.inspector.extract_page`
captures only what's visible at page-load time. Collapsed dropdowns,
filter panels that open on click, chat response areas that render after
a send, detail panels that appear after selecting a row — none of that
is in the initial DOM, so none of it ends up in the POM, so downstream
test generation has nothing to bind against.

This module layers a second pass: for each button on the page that
LOOKS like it reveals local UI (Filter, Sort, More, Options, any
aria-haspopup control, etc.), click it, wait briefly for the panel to
render, scrape the DOM delta, and merge the revealed elements into
the catalog tagged with ``revealed_by=<button_id>``. After scraping,
the panel is dismissed (Escape, then re-click the trigger) so the
next candidate is unaffected.

Safety:

* A hard allow-list (REVEAL_NAME_HINTS + aria-haspopup) — only clicks
  buttons whose name strongly suggests they open a panel. Nav buttons,
  submit buttons, and destructive buttons are not candidates.
* A deny-list (DENY_NAME_HINTS) that OVERRIDES the allow-list — no
  matter how "Filter"-ish a button's name is, if it also contains
  "delete" / "submit" / "logout" it's skipped.
* Bounded candidate count via ``settings.extraction.interactive_max_candidates``
  (default 10) so the interactive pass cannot run forever.
* Dismissal fallback chain. If a reveal click opens a panel that can't
  be closed, the rest of the pass is aborted — a stuck-open panel
  would corrupt subsequent candidates' DOM deltas.
* All exceptions are caught and logged at debug level; the interactive
  pass NEVER raises to the caller. The worst failure mode is that the
  POM ends up with the same elements the initial scrape produced.
"""

from __future__ import annotations

from typing import Iterable

from playwright.sync_api import Page, TimeoutError as PWTimeout

from autocoder import logger
from autocoder.extract.selectors import build_selector
from autocoder.models import Element, SelectorStrategy, StableSelector


# ---------------------------------------------------------------------------
# Allow / deny lists
# ---------------------------------------------------------------------------


# Button-name tokens that strongly suggest the button opens a local
# panel / dropdown / menu. Matched case-insensitively as substrings of
# the visible accessible name. Keep the list focused on verbs+nouns
# with unambiguous "opens UI" semantics.
REVEAL_NAME_HINTS: tuple[str, ...] = (
    "filter", "sort", "options", "more", "view", "show", "display",
    "choose", "select", "menu", "settings", "preferences", "customize",
    "column", "actions", "expand", "details", "info",
)


# Button-name tokens that should NEVER be clicked by the interactive
# pass — overrides the allow-list. Covers destructive, navigating,
# mutating, and auth-related actions. A button that matches BOTH lists
# is skipped (deny wins).
DENY_NAME_HINTS: tuple[str, ...] = (
    # destructive
    "delete", "remove", "drop", "clear", "reset", "discard",
    # auth / session
    "logout", "sign out", "log out", "signout",
    "login", "sign in", "log in", "signin",
    "register", "create account",
    # mutating submit
    "submit", "save", "send", "post", "publish", "approve", "reject",
    "apply",   # often a destructive "apply changes"; skip to be safe
    # navigation away
    "back", "next page", "previous page", "home", "dashboard",
    # creation
    "new", "create", "add",
    # modals / cancellation
    "cancel", "close", "dismiss",
)


# Lightweight fingerprint used to detect which interactive elements
# appeared ONLY after the reveal click. Must be cheap enough to run
# twice per candidate without bloating extraction time.
_FINGERPRINT_SELECTOR = (
    "button, a[href], input:not([type=hidden]), textarea, select, "
    "[role=button], [role=link], [role=menuitem], [role=option], "
    "[role=checkbox], [role=radio], [role=tab]"
)


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------


def _name_matches_any(name: str, hints: Iterable[str]) -> bool:
    if not name:
        return False
    lname = name.lower()
    return any(h in lname for h in hints)


def _is_deny_listed(name: str) -> bool:
    return _name_matches_any(name or "", DENY_NAME_HINTS)


def _is_reveal_candidate(element: Element) -> bool:
    """Heuristic: does this element look like it opens a local panel?"""
    if element.kind not in ("button", "link"):
        # Only buttons and links trigger reveals. Inputs / checkboxes
        # don't open panels in our threat model.
        return False
    name = (element.name or "").strip()
    if not name:
        return False
    if _is_deny_listed(name):
        return False
    # Name-based match. aria-haspopup detection happens later via
    # Playwright once we re-locate the element.
    return _name_matches_any(name, REVEAL_NAME_HINTS)


def find_reveal_candidates(elements: list[Element]) -> list[Element]:
    """Subset of ``elements`` that the interactive pass will try to click.

    Order is preserved (matches DOM order in the original scrape), so
    the pass clicks top-to-bottom in the UI. That tends to produce
    scrutable logs and avoids surprising interactions between panels.
    """
    return [e for e in elements if _is_reveal_candidate(e)]


# ---------------------------------------------------------------------------
# DOM fingerprint + dismissal
# ---------------------------------------------------------------------------


def _interactive_fingerprint(page: Page) -> set[str]:
    """Snapshot of interactive elements' outerHTML prefixes.

    Returns a set of 60-char outerHTML prefixes — cheap-to-compute,
    unique enough to identify "this element is the same as before" for
    the vast majority of pages. A delta between two snapshots names
    the elements that appeared or disappeared because of a click.
    """
    try:
        fingerprints = page.evaluate(
            """(sel) => {
                const nodes = document.querySelectorAll(sel);
                const out = [];
                for (const n of nodes) {
                    const html = (n.outerHTML || '').slice(0, 60);
                    if (html) out.push(html);
                }
                return out;
            }""",
            _FINGERPRINT_SELECTOR,
        )
        return set(fingerprints or [])
    except Exception as exc:  # noqa: BLE001
        logger.debug("interactive_fingerprint_failed", err=str(exc)[:80])
        return set()


def _dismiss_panel(page: Page, trigger_handle) -> bool:
    """Best-effort: close a revealed panel before the next candidate.

    Order:
      1. Press Escape (handles popovers, most dropdowns, dialogs).
      2. Re-click the revealing element (toggles).

    Returns True if we believe the panel is dismissed (either strategy
    succeeded OR never opened anything sticky). False if we cannot tell
    — the caller should abort the interactive pass to avoid corrupting
    subsequent candidates.
    """
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    try:
        page.wait_for_timeout(300)
    except Exception:
        pass

    # Try toggling the trigger off by clicking it again.
    try:
        trigger_handle.click(timeout=2000)
        page.wait_for_timeout(300)
    except Exception:
        # A failed re-click isn't necessarily a problem — the panel
        # may have already closed via Escape. Continue.
        pass

    return True


# ---------------------------------------------------------------------------
# Single-candidate expansion
# ---------------------------------------------------------------------------


def _resolve_selector(page: Page, sel: StableSelector):
    """Turn a :class:`StableSelector` into a Playwright Locator.

    Mirrors the subset of the runtime resolver we need here — we
    avoid pulling in the self-healing stack for a best-effort
    extraction pass.
    """
    if sel.strategy == SelectorStrategy.TEST_ID:
        return page.get_by_test_id(sel.value)
    if sel.strategy == SelectorStrategy.ROLE_NAME:
        if sel.name:
            return page.get_by_role(sel.value, name=sel.name)
        return page.get_by_role(sel.value)
    if sel.strategy == SelectorStrategy.LABEL:
        return page.get_by_label(sel.value)
    if sel.strategy == SelectorStrategy.PLACEHOLDER:
        return page.get_by_placeholder(sel.value)
    if sel.strategy == SelectorStrategy.TEXT:
        return page.get_by_text(sel.value, exact=False)
    if sel.strategy == SelectorStrategy.CSS:
        return page.locator(sel.value)
    if sel.strategy == SelectorStrategy.XPATH:
        return page.locator(f"xpath={sel.value}")
    return page.locator(sel.value)


def _locate_candidate(page: Page, candidate: Element):
    """Re-locate the candidate on the live page via its stored selector.

    We do not reuse the handle captured during the initial scrape —
    handles can be detached by the time we get here (React re-renders,
    etc.), which throws on click. Resolving by selector every time is
    slower but far more reliable. Tries the primary selector first,
    then each fallback in order, returning the first one that matches
    exactly one visible element.
    """
    specs: list[StableSelector] = [candidate.selector, *candidate.fallbacks]
    for sel in specs:
        try:
            locator = _resolve_selector(page, sel).first
            # Confirm the selector resolves to something. ``count()``
            # returns 0 when the selector is valid but no element
            # matches — treat that as a miss and try the next fallback.
            if locator.count() == 0:
                continue
            return locator
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "interactive_locate_try_failed",
                candidate_id=candidate.id,
                strategy=sel.strategy.value,
                err=str(exc)[:80],
            )
            continue
    logger.debug(
        "interactive_locate_failed",
        candidate_id=candidate.id,
        tried=len(specs),
    )
    return None


def _scrape_revealed(
    page: Page,
    pre_fingerprint: set[str],
    revealed_by_id: str,
    used_ids: set[str],
) -> list[Element]:
    """Scrape the DOM delta — elements whose fingerprint is NEW since the click."""
    from autocoder.extract.inspector import _kind_for, _short, _element_id

    try:
        nodes = page.query_selector_all(_FINGERPRINT_SELECTOR)
    except Exception:
        return []

    # Build a Counter-compatible tracker using the caller's used_ids set.
    from collections import Counter
    used_counter: Counter[str] = Counter()
    for eid in used_ids:
        used_counter[eid] = 1

    revealed: list[Element] = []
    for handle in nodes:
        try:
            html_prefix = (handle.evaluate("el => (el.outerHTML || '').slice(0, 60)") or "").strip()
        except Exception:
            continue
        if not html_prefix or html_prefix in pre_fingerprint:
            continue
        # This element is new since the click.
        try:
            if not handle.is_visible():
                continue
        except Exception:
            continue
        try:
            primary, fallbacks = build_selector(handle)
        except Exception:
            continue
        try:
            tag = (handle.evaluate("el => el.tagName") or "").lower()
        except Exception:
            tag = ""
        kind_str = _kind_for(primary.role, tag)
        seed = primary.name or primary.value or f"{tag}_revealed"
        eid = _element_id(seed, used_counter)
        # Update the shared used_ids set so the outer loop sees the
        # allocated id and doesn't re-issue it on the next candidate.
        used_ids.add(eid)
        try:
            enabled = handle.is_enabled()
        except Exception:
            enabled = True
        try:
            visible = handle.is_visible()
        except Exception:
            visible = True
        revealed.append(
            Element(
                id=eid,
                role=primary.role or kind_str,
                name=_short(primary.name, 80),
                kind=kind_str,
                selector=primary,
                fallbacks=fallbacks,
                visible=visible,
                enabled=enabled,
                revealed_by=revealed_by_id,
            )
        )
    return revealed


class _NavigatedAway(Exception):
    """Raised inside _expand_one when a reveal click caused navigation.

    The interactive pass driver catches this and aborts the entire
    pass for this page — once we've left the page, every subsequent
    candidate's handle is detached and the DOM delta is meaningless.
    """


def _expand_one(
    page: Page,
    candidate: Element,
    used_ids: set[str],
) -> list[Element]:
    """Click ``candidate``, scrape the DOM delta, dismiss, return revealed elements.

    Raises :class:`_NavigatedAway` when the click caused the page URL
    to change — a sign the candidate was actually a nav/submit action
    rather than a local panel trigger. The driver catches this and
    aborts the rest of the interactive pass.
    """
    trigger = _locate_candidate(page, candidate)
    if trigger is None:
        return []

    pre = _interactive_fingerprint(page)
    pre_url = page.url

    try:
        trigger.click(timeout=3_000)
    except PWTimeout:
        logger.debug("interactive_click_timeout", candidate_id=candidate.id)
        return []
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "interactive_click_failed",
            candidate_id=candidate.id,
            err=str(exc)[:80],
        )
        return []

    # Brief settle so the panel has time to render / animate in.
    try:
        page.wait_for_timeout(2_000)
    except Exception:
        pass

    # URL-change guard. If the click navigated the browser off the
    # page we were extracting, anything we scrape now belongs to a
    # different page — merging it into this page's POM would be
    # the same mislabeling bug the URL-path-mismatch check guards
    # against. Raise so the driver can abort the rest of the pass
    # and so the caller can try to recover the original URL.
    try:
        post_url = page.url
    except Exception:
        post_url = pre_url
    if post_url != pre_url:
        logger.warn(
            "interactive_navigated_away",
            trigger=candidate.id,
            trigger_name=candidate.name or "",
            pre_url=logger.safe_url(pre_url),
            post_url=logger.safe_url(post_url),
            hint=(
                "candidate click caused a page navigation — aborting the "
                "interactive pass for this URL. This button is a nav / "
                "submit action, not a local panel trigger."
            ),
        )
        raise _NavigatedAway(f"{candidate.id} navigated to {post_url}")

    revealed = _scrape_revealed(page, pre, candidate.id, used_ids)

    if revealed:
        logger.info(
            "interactive_revealed",
            trigger=candidate.id,
            trigger_name=candidate.name or "",
            new_elements=len(revealed),
        )
    else:
        logger.debug(
            "interactive_no_reveal",
            trigger=candidate.id,
            trigger_name=candidate.name or "",
        )

    _dismiss_panel(page, trigger)
    return revealed


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def interactive_expand(
    page: Page,
    elements: list[Element],
    *,
    max_candidates: int = 10,
) -> list[Element]:
    """Run the interactive expansion pass and return only the NEW elements.

    The caller is responsible for appending the returned list to the
    existing ``elements`` list in extraction order. Returned elements
    carry ``revealed_by`` pointing at the triggering element's id, so
    downstream POM planning can group related actions (e.g., "open
    filter menu + pick Active" becomes a two-step method chain).

    Catches every exception — a failure in this pass MUST NOT fail the
    extraction. Worst-case we return an empty list and the POM falls
    back to the initial-scrape elements.
    """
    try:
        candidates = find_reveal_candidates(elements)
    except Exception as exc:  # noqa: BLE001
        logger.debug("interactive_find_candidates_failed", err=str(exc)[:80])
        return []

    if not candidates:
        logger.debug("interactive_no_candidates")
        return []

    candidates = candidates[:max_candidates]
    logger.info(
        "interactive_pass_start",
        candidates=len(candidates),
        names=",".join((c.name or c.id)[:20] for c in candidates[:5]),
    )

    used_ids: set[str] = {e.id for e in elements}
    all_revealed: list[Element] = []
    starting_url = ""
    try:
        starting_url = page.url
    except Exception:
        pass

    for candidate in candidates:
        try:
            revealed = _expand_one(page, candidate, used_ids)
        except _NavigatedAway:
            # The candidate click navigated the browser off the
            # extraction page. Abort the rest of the interactive
            # pass — any further candidates would operate on the
            # wrong page. Attempt to restore the original URL so the
            # caller's post-interactive scrape (and the URL-path-
            # mismatch guard in the orchestrator) see the correct
            # landing.
            if starting_url:
                try:
                    page.goto(starting_url, wait_until="domcontentloaded", timeout=15_000)
                    logger.info(
                        "interactive_restored_url",
                        url=logger.safe_url(starting_url),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warn(
                        "interactive_restore_failed",
                        url=logger.safe_url(starting_url),
                        err=str(exc)[:120],
                    )
            break
        except Exception as exc:  # noqa: BLE001
            # One bad candidate (other than navigation) should not
            # abort the whole pass — log and continue.
            logger.debug(
                "interactive_expand_failed",
                candidate_id=candidate.id,
                err=str(exc)[:80],
            )
            continue
        all_revealed.extend(revealed)

    logger.info(
        "interactive_pass_done",
        tried=len(candidates),
        revealed=len(all_revealed),
    )
    return all_revealed
