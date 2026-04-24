"""Generated auth setup (Microsoft SSO flow). Captures storage_state to C:/Optimizing_autocoder-2.0/suryansh_repo/autocoder/.auth/user.json."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests import settings


_STORAGE_STATE = Path('C:\\Optimizing_autocoder-2.0\\suryansh_repo\\autocoder\\.auth\\user.json')
# Login URL is sourced from ``.env`` via ``settings.LOGIN_URL`` so the
# same test can run against dev / staging / prod without regeneration.
# The URL captured at generation time was: https://aps-aitl-frontend-bja4eebjg6cyguea.northcentralus-01.azurewebsites.net/login
_LOGIN_URL = settings.LOGIN_URL or 'https://aps-aitl-frontend-bja4eebjg6cyguea.northcentralus-01.azurewebsites.net/login'
_ENTRA_HOST = "login.microsoftonline.com"

_SESSION_STORAGE_COMPANION = _STORAGE_STATE.with_name(
    _STORAGE_STATE.stem + ".session_storage" + _STORAGE_STATE.suffix
)


def _dump_session_storage(page: Page) -> None:
    """Snapshot window.sessionStorage next to storage_state.

    Playwright's storage_state() only captures cookies + localStorage.
    MSAL.js (and most SPA auth libs) put the authenticated account in
    sessionStorage, so without this snapshot the next browser context
    would show an unauthenticated shell even with cookies present.
    """
    import json as _json

    try:
        raw = page.evaluate(
            "() => {"
            "  const out = {};"
            "  for (let i = 0; i < window.sessionStorage.length; i++) {"
            "    const k = window.sessionStorage.key(i);"
            "    out[k] = window.sessionStorage.getItem(k);"
            "  }"
            "  return out;"
            "}"
        )
    except Exception:
        return
    if not isinstance(raw, dict):
        return
    _SESSION_STORAGE_COMPANION.parent.mkdir(parents=True, exist_ok=True)
    _SESSION_STORAGE_COMPANION.write_text(
        _json.dumps(raw, ensure_ascii=False), encoding="utf-8"
    )


def _wait_for_msal_hydration(page: Page, timeout_ms: int = 20_000) -> None:
    """Block until sessionStorage has something in it.

    After an SSO popup closes, MSAL needs a tick to process the token
    response and write the authenticated account into sessionStorage on
    the main tab. Capturing storage_state before that lands gives a
    useless snapshot; this wait ensures the snapshot has teeth.
    """
    try:
        page.wait_for_function(
            "() => window.sessionStorage && window.sessionStorage.length > 0",
            timeout=timeout_ms,
        )
    except Exception:
        # MSAL-less apps simply never populate sessionStorage; that's
        # fine — their auth rides on cookies alone.
        return


def _click_kmsi(page: Page) -> None:
    """Best-effort click through the 'Stay signed in?' prompt."""
    for sel in ("#idSIButton9", "#acceptButton", 'input[type="submit"]'):
        try:
            loc = page.locator(sel).first
            if loc.count() > 0 and loc.is_visible():
                loc.click(timeout=5_000)
                return
        except Exception:
            continue


@pytest.mark.auth_setup
def test_auth_setup(page: Page, context) -> None:
    username = settings.get_required("LOGIN_USERNAME")
    password = settings.get_required("LOGIN_PASSWORD")

    page.goto(_LOGIN_URL)

    # Some apps open Entra in a popup; capture either shape.
    popup_holder: dict = {"p": None}
    context.on("page", lambda p: popup_holder.__setitem__("p", p))

    page.get_by_role('button', name='Sign in with Microsoft').click()
    page.wait_for_timeout(1500)
    active: Page = popup_holder["p"] or page

    # Wait for Entra to mount.
    for _ in range(60):
        if _ENTRA_HOST in (active.url or ""):
            break
        active = popup_holder["p"] or page
        active.wait_for_timeout(500)

    # Email + Next.
    try:
        active.locator('input[name="loginfmt"], input[type="email"]').first.fill(username, timeout=30_000)
        active.locator('#idSIButton9, input[type="submit"], button[type="submit"]').first.click(timeout=10_000)
    except Exception:
        pass

    # Password + Sign in.
    active.locator('input[name="passwd"], input[type="password"]').first.fill(password, timeout=45_000)
    active.locator('#idSIButton9, input[type="submit"], button[type="submit"]').first.click(timeout=10_000)

    _click_kmsi(active)

    # If Entra was driven in a popup, bring focus back to the main tab.
    # The main page is where MSAL writes the account into sessionStorage
    # and is the origin whose cookies we need to persist.
    try:
        page.bring_to_front()
    except Exception:
        pass

    page.wait_for_url(lambda url: 'https://aps-aitl-frontend-bja4eebjg6cyguea.northcentralus-01.azurewebsites.net' in url, timeout=120_000)

    # Give the main-tab MSAL client a beat to process the token response
    # and hydrate sessionStorage before we snapshot it.
    _wait_for_msal_hydration(page)

    _STORAGE_STATE.parent.mkdir(parents=True, exist_ok=True)
    page.context.storage_state(path=str(_STORAGE_STATE))
    _dump_session_storage(page)
