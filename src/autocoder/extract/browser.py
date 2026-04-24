"""Tiny wrapper around Playwright's sync API.

The orchestrator runs synchronously to keep the control flow obvious.
We expose a context manager that handles browser/context/page lifecycle
and (optionally) loads an authenticated ``storage_state`` so protected
pages can be explored.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PWError,
    Page,
    Playwright,
    TimeoutError as PWTimeout,
    sync_playwright,
)

from autocoder import logger
from autocoder.config import Settings


@dataclass
class BrowserSession:
    pw: Playwright
    browser: Browser
    context: BrowserContext
    page: Page


@dataclass
class NavDiagnostics:
    """Everything we captured during a resilient navigation attempt."""

    final_url: str = ""
    status: int | None = None
    redirects: list[str] = field(default_factory=list)
    popup_urls: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    wait_strategy: str = ""
    elapsed_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "final_url": logger.safe_url(self.final_url),
            "status": self.status,
            "redirects": [logger.safe_url(u) for u in self.redirects[:8]],
            "popup_urls": [logger.safe_url(u) for u in self.popup_urls[:3]],
            "console_errors": self.console_errors[:5],
            "failed_requests": self.failed_requests[:5],
            "wait_strategy": self.wait_strategy,
            "elapsed": f"{self.elapsed_s:.2f}s",
        }


class AuthUnreachable(RuntimeError):
    """Raised when a resilient goto times out with no usable DOM.

    Carries the :class:`NavDiagnostics` captured during the attempt so
    callers can decide whether to escalate into the auth-first flow.
    """

    def __init__(self, url: str, diag: NavDiagnostics):
        super().__init__(f"navigation timed out for {url}")
        self.url = url
        self.diag = diag


@contextmanager
def open_session(
    settings: Settings,
    *,
    use_storage_state: bool = False,
):
    """Open a Chromium session.

    ``use_storage_state=True`` injects ``settings.paths.storage_state``
    if the file exists. We never *fail* when missing — that path is
    populated by the auth-setup test, which has to run at least once.
    """
    storage_state: str | None = None
    sp = settings.paths.storage_state
    if use_storage_state:
        if sp.exists() and sp.stat().st_size > 0:
            storage_state = str(sp)
            logger.debug("browser_storage_loaded", path=str(sp))
        else:
            logger.warn(
                "browser_storage_missing",
                path=str(sp),
                hint="Run `pytest tests/auth_setup -m auth_setup` first.",
            )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=settings.browser.headless)
        ctx_kwargs: dict = {}
        if storage_state:
            ctx_kwargs["storage_state"] = storage_state
        context = browser.new_context(**ctx_kwargs)
        context.set_default_timeout(settings.browser.extraction_timeout_ms)
        context.set_default_navigation_timeout(settings.browser.extraction_nav_timeout_ms)
        page = context.new_page()
        logger.debug(
            "browser_session_open",
            headless=settings.browser.headless,
            storage_state=bool(storage_state),
        )
        try:
            yield BrowserSession(pw=pw, browser=browser, context=context, page=page)
        finally:
            context.close()
            browser.close()
            logger.debug("browser_session_closed")


def ensure_storage_state_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def open_shared_session(settings: Settings, *, use_storage_state: bool = False):
    """Open a Playwright context that **stays open** across multiple URLs.

    ``open_session`` is one-shot: it opens a browser, does the thing,
    and closes. That loses every piece of in-memory session state
    that MSAL (and similar SPA auth libraries) keep outside cookies:

    * ``sessionStorage`` — where MSAL stores its authenticated account.
    * in-memory token cache of the MSAL JS client.
    * service-worker-held state.

    Playwright's ``context.storage_state(path=...)`` only captures
    cookies and ``localStorage``. So a run that closes the auth
    context before navigating to the real URLs forces every URL to
    show its pre-auth consent shell — the SPA starts fresh and MSAL
    can't find an account.

    This session context manager is intended to span the entire
    auth-first + per-URL extraction phase. Call it once at the top
    of the orchestrator, pass it into the auth runner and into every
    ``_extract_detailed`` call. The browser remains open through the
    whole authenticated lifecycle; we only close it at the end, and
    we save ``storage_state`` on exit so the file on disk reflects
    any cookies refreshed mid-run.

    Yields the same :class:`BrowserSession` dataclass as
    :func:`open_session` for drop-in compatibility.
    """
    storage_state: str | None = None
    sp = settings.paths.storage_state
    if use_storage_state and sp.exists() and sp.stat().st_size > 0:
        storage_state = str(sp)
        logger.debug("shared_storage_loaded", path=str(sp))

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.launch(headless=settings.browser.headless)
        ctx_kwargs: dict = {}
        if storage_state:
            ctx_kwargs["storage_state"] = storage_state
        context = browser.new_context(**ctx_kwargs)
        context.set_default_timeout(settings.browser.extraction_timeout_ms)
        context.set_default_navigation_timeout(settings.browser.extraction_nav_timeout_ms)
        page = context.new_page()
        logger.info(
            "shared_session_opened",
            headless=settings.browser.headless,
            storage_state=bool(storage_state),
        )
        try:
            yield BrowserSession(pw=pw, browser=browser, context=context, page=page)
        finally:
            # Persist storage state before teardown so any cookie
            # refresh the app issued during the run is not lost. We
            # also snapshot sessionStorage to a companion file
            # (MSAL.js keeps its authenticated account there and
            # Playwright does not persist it by default).
            try:
                sp.parent.mkdir(parents=True, exist_ok=True)
                context.storage_state(path=str(sp))
                logger.debug("shared_storage_saved", path=str(sp))
            except Exception:
                pass
            try:
                from autocoder.extract.auth_runner import _save_session_storage

                _save_session_storage(page, sp)
            except Exception:
                pass
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
            logger.info("shared_session_closed")
    finally:
        pw.stop()


def goto_resilient(
    page: Page,
    url: str,
    *,
    nav_timeout_ms: int,
    diagnostics_dir: Path | None = None,
    capture_on_timeout: bool = True,
) -> NavDiagnostics:
    """Navigate with a tiered wait strategy and capture diagnostics.

    Why: ``wait_until="domcontentloaded"`` is fragile for SPAs that
    immediately redirect to a different origin (SSO, auth middleware).
    We try ``commit`` first (fires as soon as the first response byte
    is received), then *best-effort* escalate to ``domcontentloaded``
    and ``networkidle`` within bounded budgets. If even ``commit``
    cannot complete we raise :class:`AuthUnreachable` with a diagnostic
    payload so the orchestrator can decide whether to run auth-first.
    """
    diag = NavDiagnostics()
    started = time.monotonic()

    def _on_console(msg) -> None:
        try:
            if msg.type in {"error", "warning"}:
                diag.console_errors.append(f"{msg.type}:{msg.text[:160]}")
        except Exception:
            pass

    def _on_request_failed(req) -> None:
        try:
            diag.failed_requests.append(
                f"{req.method} {logger.safe_url(req.url)} {req.failure or ''}".strip()
            )
        except Exception:
            pass

    def _on_frame_nav(frame) -> None:
        try:
            if frame == page.main_frame:
                diag.redirects.append(frame.url)
        except Exception:
            pass

    def _on_popup(popup) -> None:
        try:
            diag.popup_urls.append(popup.url or "")
        except Exception:
            pass

    page.on("console", _on_console)
    page.on("requestfailed", _on_request_failed)
    page.on("framenavigated", _on_frame_nav)
    page.on("popup", _on_popup)

    # Tier 1: commit — as soon as the first response byte arrives.
    #
    # MSAL-style SPAs frequently intercept the first navigation with a
    # token-refresh redirect (the browser URL changes to an OAuth
    # callback like ``/#code=...&state=...`` mid-goto), which surfaces
    # to Playwright as:
    #   ``Page.goto: Navigation to "<url>" is interrupted by another
    #   navigation to "<callback>"``
    # This is NOT a failure — the SPA will land back at the callback,
    # finish the handshake, and settle. We catch that specific error,
    # wait briefly for the handshake to resolve, and re-issue the
    # original goto once.
    def _do_goto() -> Any:
        return page.goto(url, wait_until="commit", timeout=nav_timeout_ms)

    try:
        resp = _do_goto()
        diag.wait_strategy = "commit"
        diag.status = getattr(resp, "status", None) if resp is not None else None
    except PWTimeout:
        # If commit itself cannot land, there is no usable DOM.
        diag.final_url = page.url or url
        diag.elapsed_s = time.monotonic() - started
        if capture_on_timeout and diagnostics_dir is not None:
            _dump_timeout_artifacts(page, url, diagnostics_dir)
        raise AuthUnreachable(url, diag) from None
    except PWError as exc:
        if "interrupted by another navigation" not in str(exc):
            raise
        # Let the MSAL / OIDC callback finish resolving before we
        # re-navigate. domcontentloaded usually fires within 5–10s
        # on the callback URL; if it doesn't, we proceed anyway.
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15_000)
        except PWTimeout:
            pass
        logger.info(
            "goto_redirect_retry",
            url=logger.safe_url(url),
            callback_url=logger.safe_url(page.url or ""),
            hint="MSAL/OIDC redirect interrupted the first goto; re-issuing after callback settled",
        )
        try:
            resp = _do_goto()
            diag.wait_strategy = "commit"
            diag.status = getattr(resp, "status", None) if resp is not None else None
        except (PWTimeout, PWError):
            # Second attempt also failed — surface an auth-unreachable
            # diagnostic so the orchestrator can route to auth-first.
            diag.final_url = page.url or url
            diag.elapsed_s = time.monotonic() - started
            if capture_on_timeout and diagnostics_dir is not None:
                _dump_timeout_artifacts(page, url, diagnostics_dir)
            raise AuthUnreachable(url, diag) from None

    # Tier 2: best-effort domcontentloaded.
    try:
        page.wait_for_load_state(
            "domcontentloaded", timeout=max(nav_timeout_ms // 2, 5_000)
        )
        diag.wait_strategy = "commit+domcontentloaded"
    except PWTimeout:
        pass

    # Tier 3: best-effort networkidle. Authenticated SPAs commonly
    # fetch multiple XHRs on mount (auth refresh, user profile, first-
    # page data) that don't settle inside a 5s budget; 15s handles the
    # typical mid-sized app without penalizing pages that already
    # idle quickly (the wait returns as soon as 500ms of network
    # quiet is observed, regardless of the budget).
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
        diag.wait_strategy = diag.wait_strategy + "+networkidle"
    except PWTimeout:
        pass

    diag.final_url = page.url
    diag.elapsed_s = time.monotonic() - started
    return diag


def _dump_timeout_artifacts(page: Page, url: str, out_dir: Path) -> None:
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe_host = logger.safe_url(url).split("//")[-1].split("/")[0] or "page"
        base = out_dir / f"nav_timeout_{stamp}_{safe_host}"
        try:
            page.screenshot(path=str(base) + ".png", full_page=True)
        except Exception:
            pass
        try:
            (Path(str(base) + ".html")).write_text(
                page.content() or "", encoding="utf-8"
            )
        except Exception:
            pass
        logger.warn("nav_timeout_artifacts", base=str(base))
    except Exception:
        pass
