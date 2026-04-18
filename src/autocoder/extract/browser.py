"""Tiny wrapper around Playwright's sync API.

The orchestrator runs synchronously to keep the control flow obvious.
We expose a context manager that handles browser/context/page lifecycle
and (optionally) loads an authenticated ``storage_state`` so protected
pages can be explored.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright

from autocoder import logger
from autocoder.config import Settings


@dataclass
class BrowserSession:
    pw: Playwright
    browser: Browser
    context: BrowserContext
    page: Page


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
