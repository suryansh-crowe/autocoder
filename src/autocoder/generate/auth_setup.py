"""Render the auth-setup test that captures storage_state.

The output runs as a Playwright project named ``auth-setup`` (see
``tests/conftest.py``). It executes once before the rest of the suite,
fills the detected login form using credentials read from the env, and
writes ``.auth/user.json`` for protected projects to consume.

The renderer never embeds secrets in the file. It only emits
``os.environ.get(...)`` lookups with helpful error messages when they
are absent.
"""

from __future__ import annotations

from autocoder.models import AuthSpec, SelectorStrategy, StableSelector


_TEMPLATE = '''"""Generated auth setup. Captures storage_state to {storage_state_path}."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect


_STORAGE_STATE = Path({storage_state_path!r})
_LOGIN_URL = {login_url!r}


def _need(env: str) -> str:
    val = os.environ.get(env, "").strip()
    if not val:
        raise RuntimeError(f"Missing required env var: {{env}}. Add it to .env (never commit it).")
    return val


@pytest.mark.auth_setup
def test_auth_setup(page: Page) -> None:
    username = _need("{username_env}")
    password = _need("{password_env}")

    page.goto(_LOGIN_URL)
    {username_locator}.fill(username)
    {password_locator}.fill(password)
    {submit_locator}.click()

    {wait_block}

    _STORAGE_STATE.parent.mkdir(parents=True, exist_ok=True)
    page.context.storage_state(path=str(_STORAGE_STATE))
'''


def _render_locator(sel: StableSelector | None, fallback: str) -> str:
    if sel is None:
        return f"page.locator({fallback!r})"
    if sel.strategy == SelectorStrategy.TEST_ID:
        return f"page.get_by_test_id({sel.value!r})"
    if sel.strategy == SelectorStrategy.ROLE_NAME:
        if sel.name:
            return f"page.get_by_role({sel.value!r}, name={sel.name!r})"
        return f"page.get_by_role({sel.value!r})"
    if sel.strategy == SelectorStrategy.LABEL:
        return f"page.get_by_label({sel.value!r})"
    if sel.strategy == SelectorStrategy.PLACEHOLDER:
        return f"page.get_by_placeholder({sel.value!r})"
    if sel.strategy == SelectorStrategy.TEXT:
        return f"page.get_by_text({sel.value!r}, exact=False)"
    if sel.strategy == SelectorStrategy.CSS:
        return f"page.locator({sel.value!r})"
    if sel.strategy == SelectorStrategy.XPATH:
        return f"page.locator('xpath={sel.value}')"
    return f"page.locator({fallback!r})"


def render_auth_setup(spec: AuthSpec, *, storage_state_path: str) -> str:
    if spec.success_indicator_url_contains:
        marker = spec.success_indicator_url_contains
        wait_block = f"page.wait_for_url(lambda url: {marker!r} in url, timeout=60_000)"
    elif spec.success_indicator_text:
        wait_block = (
            f"expect(page.get_by_text({spec.success_indicator_text!r}, exact=False)).to_be_visible()"
        )
    else:
        wait_block = "page.wait_for_load_state('networkidle')"

    return _TEMPLATE.format(
        storage_state_path=storage_state_path,
        login_url=spec.login_url,
        username_env=spec.username_env,
        password_env=spec.password_env,
        username_locator=_render_locator(spec.username_selector, "input[name=username]"),
        password_locator=_render_locator(spec.password_selector, "input[type=password]"),
        submit_locator=_render_locator(spec.submit_selector, "button[type=submit]"),
        wait_block=wait_block,
    )
