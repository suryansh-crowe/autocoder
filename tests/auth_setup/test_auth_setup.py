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


def _skip_if_session_captured() -> None:
    """Skip the test when `.auth/user.json` already exists and is non-empty.

    `autocoder run` and the conftest `_ensure_auth_session` fixture both
    capture storage_state in-process. Re-running this test after that
    is redundant and fails on SSO/passwordless tenants where some env
    vars (e.g. LOGIN_PASSWORD) are deliberately unset. Delete
    `.auth/user.json` to force a fresh capture.
    """
    if _STORAGE_STATE.exists() and _STORAGE_STATE.stat().st_size > 0:
        pytest.skip(
            f"storage_state already captured at {_STORAGE_STATE} — "
            "delete the file to force a fresh capture."
        )


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
    _skip_if_session_captured()
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

    page.wait_for_load_state('networkidle')

    _STORAGE_STATE.parent.mkdir(parents=True, exist_ok=True)
    page.context.storage_state(path=str(_STORAGE_STATE))
