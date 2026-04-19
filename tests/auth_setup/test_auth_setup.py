"""Generated auth setup (Microsoft SSO flow). Captures storage_state to C:/Users/singhs14/OneDrive - Crowe LLP/Desktop/autocoder/.auth/user.json."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect


_STORAGE_STATE = Path('C:\\Users\\singhs14\\OneDrive - Crowe LLP\\Desktop\\autocoder\\.auth\\user.json')
_LOGIN_URL = 'https://aps-aitl-frontend-bja4eebjg6cyguea.northcentralus-01.azurewebsites.net/login'
_ENTRA_HOST = "login.microsoftonline.com"


def _need(env: str) -> str:
    val = os.environ.get(env, "").strip()
    if not val:
        raise RuntimeError(f"Missing required env var: {env}. Add it to .env (never commit it).")
    return val


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
    username = _need("LOGIN_USERNAME")
    password = _need("LOGIN_PASSWORD")

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

    page.wait_for_url(lambda url: 'https://aps-aitl-frontend-bja4eebjg6cyguea.northcentralus-01.azurewebsites.net/stewie' in url, timeout=120_000)

    _STORAGE_STATE.parent.mkdir(parents=True, exist_ok=True)
    page.context.storage_state(path=str(_STORAGE_STATE))
