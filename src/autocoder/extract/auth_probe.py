"""Detect login form fields on the login URL.

The result feeds the auth-setup generator: if we can find a username
field, a password field, and a submit affordance, the renderer can
emit a complete `auth.setup.py` without a single LLM token.
"""

from __future__ import annotations

from playwright.sync_api import Page

from autocoder import logger
from autocoder.extract.selectors import build_selector
from autocoder.models import AuthSpec, StableSelector


_USERNAME_HINTS = ("email", "username", "user", "login", "account", "userid", "user_id")


def _first_visible(page: Page, selector: str):
    try:
        loc = page.locator(selector)
        for i in range(min(loc.count(), 5)):
            handle = loc.nth(i).element_handle()
            if handle and handle.is_visible():
                return handle
    except Exception:
        return None
    return None


def _selector_from_handle(handle) -> StableSelector | None:
    if handle is None:
        return None
    try:
        primary, _ = build_selector(handle)
        return primary
    except Exception:
        return None


def detect_login_fields(page: Page) -> tuple[StableSelector | None, StableSelector | None, StableSelector | None]:
    """Return (username, password, submit) selectors if found."""
    pwd_handle = _first_visible(page, 'input[type="password"]')
    pwd_selector = _selector_from_handle(pwd_handle)

    user_handle = None
    matched_hint = ""
    for hint in _USERNAME_HINTS:
        user_handle = _first_visible(
            page,
            f"input[type='email'], input[name*={hint} i], input[id*={hint} i], input[autocomplete*={hint} i]",
        )
        if user_handle:
            matched_hint = hint
            break
    if user_handle is None:
        # Fallback: first visible text-style input that is not the password
        user_handle = _first_visible(page, "input:not([type=hidden]):not([type=password])")
        if user_handle:
            matched_hint = "fallback:any-non-password-input"
    user_selector = _selector_from_handle(user_handle)

    submit_handle = _first_visible(page, 'button[type="submit"], input[type="submit"]')
    submit_source = "submit_button_or_input"
    if submit_handle is None:
        submit_handle = _first_visible(page, "form button")
        submit_source = "first_form_button"
    if submit_handle is None:
        submit_handle = _first_visible(page, "button")
        submit_source = "any_visible_button"
    submit_selector = _selector_from_handle(submit_handle)

    logger.debug(
        "auth_probe_fields",
        username_found=bool(user_selector),
        username_hint=matched_hint,
        password_found=bool(pwd_selector),
        submit_found=bool(submit_selector),
        submit_source=submit_source,
    )
    return user_selector, pwd_selector, submit_selector


def build_auth_spec(
    page: Page,
    *,
    login_url: str,
    storage_state_path: str,
    success_url_marker: str | None = None,
) -> AuthSpec | None:
    user, pwd, submit = detect_login_fields(page)
    if not (user and pwd and submit):
        return None
    return AuthSpec(
        login_url=login_url,
        username_selector=user,
        password_selector=pwd,
        submit_selector=submit,
        success_indicator_url_contains=success_url_marker,
        storage_state_path=storage_state_path,
    )
