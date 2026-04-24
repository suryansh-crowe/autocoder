"""Render the auth-setup test that captures storage_state.

The output runs as a Playwright project named ``auth-setup`` (see
``tests/conftest.py``). It executes once before the rest of the suite,
fills the detected login form using credentials read from the env, and
writes ``.auth/user.json`` for protected projects to consume.

Two variants exist:

* **Form** — classic ``username`` / ``password`` / ``submit``. Emitted
  when the login page exposes an inline credential form.
* **SSO (Microsoft)** — the app's login page only has a provider
  button; the actual credential entry happens on
  ``login.microsoftonline.com``. We render a script that clicks the
  provider button and drives the Entra page using the default MSAL
  selectors. Works for vanilla Entra tenants; tenants with heavy
  branding or MFA may need manual tweaks.

The renderer never embeds secrets in the file. It only emits
``tests.settings.get_required(...)`` lookups — which funnel through
the single ``.env`` loader in ``tests/settings.py`` — so the generated
test never touches ``os.environ`` directly.
"""

from __future__ import annotations

from autocoder.models import AuthSpec, SelectorStrategy, StableSelector


_SESSION_STORAGE_HELPERS = '''
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
            "() => {{"
            "  const out = {{}};"
            "  for (let i = 0; i < window.sessionStorage.length; i++) {{"
            "    const k = window.sessionStorage.key(i);"
            "    out[k] = window.sessionStorage.getItem(k);"
            "  }}"
            "  return out;"
            "}}"
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
'''


_TEMPLATE_FORM = '''"""Generated auth setup (form flow). Captures storage_state to {storage_state_display}."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests import settings


_STORAGE_STATE = Path({storage_state_path!r})
# Login URL is sourced from ``.env`` via ``settings.LOGIN_URL`` so the
# same test can run against dev / staging / prod without regeneration.
# The URL captured at generation time was: {login_url}
_LOGIN_URL = settings.LOGIN_URL or {login_url!r}
''' + _SESSION_STORAGE_HELPERS + '''

@pytest.mark.auth_setup
def test_auth_setup(page: Page) -> None:
    username = settings.get_required("{username_env}")
    password = settings.get_required("{password_env}")

    page.goto(_LOGIN_URL)
    {username_locator}.fill(username)
    {password_locator}.fill(password)
    {submit_locator}.click()

    {wait_block}

    _wait_for_msal_hydration(page)

    _STORAGE_STATE.parent.mkdir(parents=True, exist_ok=True)
    page.context.storage_state(path=str(_STORAGE_STATE))
    _dump_session_storage(page)
'''


_TEMPLATE_USERNAME_FIRST = '''"""Generated auth setup (username-first flow). Captures storage_state to {storage_state_display}."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, TimeoutError as PWTimeout, expect

from tests import settings


_STORAGE_STATE = Path({storage_state_path!r})
# Login URL is sourced from ``.env`` via ``settings.LOGIN_URL`` so the
# same test can run against dev / staging / prod without regeneration.
# The URL captured at generation time was: {login_url}
_LOGIN_URL = settings.LOGIN_URL or {login_url!r}
''' + _SESSION_STORAGE_HELPERS + '''

@pytest.mark.auth_setup
def test_auth_setup(page: Page) -> None:
    username = settings.get_required("{username_env}")
    password = settings.get_optional("{password_env}")  # optional on this flow

    page.goto(_LOGIN_URL)
    {username_locator}.fill(username)
    {continue_locator}.click()

    # Wait for the second screen. It is either a password input, an
    # IdP redirect, or a code challenge. We handle the first; the other
    # two require a real credential store and are marked for manual
    # completion.
    try:
        page.locator('input[type="password"]').first.wait_for(timeout=30_000)
    except PWTimeout:
        pytest.skip(
            "username-first flow did not reveal a password field within 30s. "
            "Finish the login manually (SSO / OTP / MFA) and rerun."
        )
    if not password:
        pytest.skip(
            "username-first flow reached a password prompt but {password_env} is not set."
        )
    page.locator('input[type="password"]').first.fill(password)
    for sel in ('button[type="submit"]', 'input[type="submit"]', '#idSIButton9'):
        loc = page.locator(sel).first
        if loc.count() > 0 and loc.is_visible():
            loc.click()
            break

    {wait_block}

    _wait_for_msal_hydration(page)

    _STORAGE_STATE.parent.mkdir(parents=True, exist_ok=True)
    page.context.storage_state(path=str(_STORAGE_STATE))
    _dump_session_storage(page)
'''


_TEMPLATE_EMAIL_ONLY = '''"""Generated auth setup (email-only / magic-link / OTP flow).

Captures storage_state to {storage_state_path} *after* the external
step (magic-link click or OTP code) has been completed. Run this test
with ``HEADLESS=false`` so you can finish the external step in the
browser window. The test pauses until the page transitions off the
login route.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests import settings


_STORAGE_STATE = Path({storage_state_path!r})
# Login URL is sourced from ``.env`` via ``settings.LOGIN_URL`` so the
# same test can run against dev / staging / prod without regeneration.
# The URL captured at generation time was: {login_url}
_LOGIN_URL = settings.LOGIN_URL or {login_url!r}
''' + _SESSION_STORAGE_HELPERS + '''

@pytest.mark.auth_setup
def test_auth_setup(page: Page) -> None:
    username = settings.get_required("{username_env}")

    page.goto(_LOGIN_URL)
    {username_locator}.fill(username)
    {continue_locator}.click()

    # External step required: the server dispatches a link or code to
    # the email above. Finish the login in the visible browser window,
    # then this test will proceed when the page leaves the login route.
    page.wait_for_url(
        lambda u: "/login" not in u and "/sign" not in u,
        timeout=300_000,
    )

    {wait_block}

    _wait_for_msal_hydration(page)

    _STORAGE_STATE.parent.mkdir(parents=True, exist_ok=True)
    page.context.storage_state(path=str(_STORAGE_STATE))
    _dump_session_storage(page)
'''


_TEMPLATE_SSO_MS = '''"""Generated auth setup (Microsoft SSO flow). Captures storage_state to {storage_state_display}."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests import settings


_STORAGE_STATE = Path({storage_state_path!r})
# Login URL is sourced from ``.env`` via ``settings.LOGIN_URL`` so the
# same test can run against dev / staging / prod without regeneration.
# The URL captured at generation time was: {login_url}
_LOGIN_URL = settings.LOGIN_URL or {login_url!r}
_ENTRA_HOST = "login.microsoftonline.com"
''' + _SESSION_STORAGE_HELPERS + '''

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
    username = settings.get_required("{username_env}")
    password = settings.get_required("{password_env}")

    page.goto(_LOGIN_URL)

    # Some apps open Entra in a popup; capture either shape.
    popup_holder: dict = {{"p": None}}
    context.on("page", lambda p: popup_holder.__setitem__("p", p))

    {sso_click_locator}.click()
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

    {wait_block}

    # Give the main-tab MSAL client a beat to process the token response
    # and hydrate sessionStorage before we snapshot it.
    _wait_for_msal_hydration(page)

    _STORAGE_STATE.parent.mkdir(parents=True, exist_ok=True)
    page.context.storage_state(path=str(_STORAGE_STATE))
    _dump_session_storage(page)
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
        wait_block = f"page.wait_for_url(lambda url: {marker!r} in url, timeout=120_000)"
    elif spec.success_indicator_text:
        wait_block = (
            f"expect(page.get_by_text({spec.success_indicator_text!r}, exact=False)).to_be_visible()"
        )
    else:
        wait_block = "page.wait_for_load_state('networkidle')"

    # Windows paths contain backslashes; dropping one verbatim into a
    # docstring makes Python parse it as a unicode escape
    # (``\U...``, ``\a``, ``\t``, ...). Use a POSIX-style rendering
    # for human-readable substitutions, and keep ``!r`` (which uses
    # ``repr`` and escapes backslashes) for the real ``Path(...)``
    # call inside the generated code.
    storage_state_display = storage_state_path.replace("\\", "/")

    if spec.auth_kind in ("sso_microsoft", "sso_generic"):
        return _TEMPLATE_SSO_MS.format(
            storage_state_path=storage_state_path,
            storage_state_display=storage_state_display,
            login_url=spec.login_url,
            username_env=spec.username_env,
            password_env=spec.password_env,
            sso_click_locator=_render_locator(
                spec.sso_button_selector, 'button:has-text("Sign in")'
            ),
            wait_block=wait_block,
        )

    if spec.auth_kind == "username_first":
        return _TEMPLATE_USERNAME_FIRST.format(
            storage_state_path=storage_state_path,
            storage_state_display=storage_state_display,
            login_url=spec.login_url,
            username_env=spec.username_env,
            password_env=spec.password_env,
            username_locator=_render_locator(
                spec.username_selector, 'input[type=email]'
            ),
            continue_locator=_render_locator(
                spec.continue_selector, 'button:has-text("Next")'
            ),
            wait_block=wait_block,
        )

    if spec.auth_kind in ("email_only", "magic_link", "otp_code"):
        return _TEMPLATE_EMAIL_ONLY.format(
            storage_state_path=storage_state_path,
            storage_state_display=storage_state_display,
            login_url=spec.login_url,
            username_env=spec.username_env,
            username_locator=_render_locator(
                spec.username_selector, 'input[type=email]'
            ),
            continue_locator=_render_locator(
                spec.continue_selector or spec.submit_selector,
                'button:has-text("Send")',
            ),
            wait_block=wait_block,
        )

    # Unknown auth shapes — still render a form-like scaffold so the
    # user has one place to start from; they will typically need to
    # hand-edit the selectors.
    return _TEMPLATE_FORM.format(
        storage_state_path=storage_state_path,
        storage_state_display=storage_state_display,
        login_url=spec.login_url,
        username_env=spec.username_env,
        password_env=spec.password_env,
        username_locator=_render_locator(spec.username_selector, "input[name=username]"),
        password_locator=_render_locator(spec.password_selector, "input[type=password]"),
        submit_locator=_render_locator(spec.submit_selector, "button[type=submit]"),
        wait_block=wait_block,
    )
