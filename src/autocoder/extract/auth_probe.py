"""Detect login page shape and infer an ``auth_kind``.

The detector is deliberately permissive: it produces *some*
:class:`AuthSpec` for every login-shaped page, even when the exact
flow cannot be automated end-to-end. The rest of the pipeline reads
``AuthSpec.auth_kind`` to decide what the runner should try, what
credentials the env must supply, and whether external completion
(magic link, OTP, MFA) is expected.

Recognised shapes
-----------------

* **form**              — username + password + submit inline.
* **username_first**    — username/email + Next/Continue button, no
                          password yet. Password typically appears on
                          a second screen or via a redirect.
* **email_only**        — email input + submit (no provider button,
                          no password). Usually a magic-link pattern.
* **magic_link**        — "Email me a login link" / "Send magic link".
* **otp_code**          — "Send me a code" / "Get code".
* **sso_microsoft**     — "Sign in with Microsoft" button.
* **sso_generic**       — other provider buttons (Google/GitHub/SSO).
* **unknown_auth**      — login-shaped page that does not match any
                          of the above. A best-effort scaffold is
                          still produced.

The first rule that matches wins. Order is: form → magic_link →
otp_code → sso_microsoft → sso_generic → username_first → email_only
→ unknown_auth.
"""

from __future__ import annotations

from playwright.sync_api import Page

from autocoder import logger
from autocoder.extract.selectors import build_selector
from autocoder.models import AuthSpec, StableSelector


_USERNAME_HINTS = ("email", "username", "user", "login", "account", "userid", "user_id")

# Second-step button text that indicates a multi-step flow.
_CONTINUE_PHRASES: tuple[str, ...] = (
    "next", "continue", "continue to", "proceed", "go",
)

_MAGIC_LINK_PHRASES: tuple[str, ...] = (
    "email me a link", "send me a link", "send magic link",
    "email me a login link", "send login link", "magic link",
    "get a login link",
)

_OTP_PHRASES: tuple[str, ...] = (
    "send me a code", "email me a code", "send code", "get code",
    "send one-time code", "send verification code", "send a code",
)

_EMAIL_INPUT_CSS = (
    'input[type="email"], '
    'input[name*="email" i], input[id*="email" i], '
    'input[autocomplete*="email" i]'
)

# Each entry: (auth_kind, match_texts, optional css anchors).
# Text matching is case-insensitive and uses substring.
_SSO_PROVIDERS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    (
        "sso_microsoft",
        (
            "sign in with microsoft",
            "sign in with work",
            "continue with microsoft",
            "login with microsoft",
            "microsoft account",
        ),
        (
            'button:has-text("Microsoft")',
            'a:has-text("Microsoft")',
            '[data-provider="microsoft" i]',
        ),
    ),
    (
        "sso_generic",
        (
            "sign in with google",
            "continue with google",
            "sign in with github",
            "continue with github",
            "sign in with sso",
            "continue with sso",
            "single sign-on",
        ),
        (
            'button:has-text("Google")',
            'button:has-text("GitHub")',
            'button:has-text("SSO")',
        ),
    ),
)


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


def _scan_sso_button(page: Page) -> tuple[str, StableSelector] | None:
    """Return ``(auth_kind, selector)`` for the first SSO button we find.

    Checks in priority order:

    1. Role-based search for ``button``/``link`` whose accessible name
       matches a known provider phrase.
    2. CSS anchors (``button:has-text(...)``, ``[data-provider=...]``)
       as a fallback for sites that misuse semantic roles.
    """
    for auth_kind, phrases, css_anchors in _SSO_PROVIDERS:
        # Role-based first — most reliable.
        for role in ("button", "link"):
            for phrase in phrases:
                try:
                    loc = page.get_by_role(role, name=_ICASE_RE(phrase))
                    if loc.count() == 0:
                        continue
                    handle = loc.first.element_handle()
                    if handle is None or not handle.is_visible():
                        continue
                    sel = _selector_from_handle(handle)
                    if sel is not None:
                        return auth_kind, sel
                except Exception:
                    continue
        # CSS fallback.
        for css in css_anchors:
            handle = _first_visible(page, css)
            if handle is None:
                continue
            sel = _selector_from_handle(handle)
            if sel is not None:
                return auth_kind, sel
    return None


def _ICASE_RE(phrase: str):
    """Playwright ``get_by_role(name=...)`` accepts a regex — build a case-insensitive one."""
    import re as _re

    return _re.compile(_re.escape(phrase), _re.IGNORECASE)


def _scan_phrase_button(
    page: Page, phrases: tuple[str, ...]
) -> StableSelector | None:
    """Return a selector for the first visible button/link matching any phrase."""
    for phrase in phrases:
        for role in ("button", "link"):
            try:
                loc = page.get_by_role(role, name=_ICASE_RE(phrase))
                if loc.count() == 0:
                    continue
                handle = loc.first.element_handle()
                if handle is None or not handle.is_visible():
                    continue
                sel = _selector_from_handle(handle)
                if sel is not None:
                    return sel
            except Exception:
                continue
        # CSS fallback — broader but less precise.
        handle = _first_visible(page, f'button:has-text("{phrase}")')
        if handle is None:
            handle = _first_visible(page, f'a:has-text("{phrase}")')
        sel = _selector_from_handle(handle)
        if sel is not None:
            return sel
    return None


def _has_email_only_input(page: Page) -> bool:
    """``True`` when the only visible text input is an email field."""
    try:
        all_inputs = page.locator(
            'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=checkbox]):not([type=radio])'
        )
        visible = []
        for i in range(min(all_inputs.count(), 8)):
            h = all_inputs.nth(i).element_handle()
            if h and h.is_visible():
                visible.append(h)
        if len(visible) != 1:
            return False
        h = visible[0]
        itype = (h.get_attribute("type") or "").lower()
        name = (h.get_attribute("name") or "").lower()
        aid = (h.get_attribute("id") or "").lower()
        autoc = (h.get_attribute("autocomplete") or "").lower()
        return (
            itype == "email"
            or "email" in name
            or "email" in aid
            or "email" in autoc
        )
    except Exception:
        return False


def build_auth_spec(
    page: Page,
    *,
    login_url: str,
    storage_state_path: str,
    success_url_marker: str | None = None,
) -> AuthSpec | None:
    """Infer auth mode and return the best :class:`AuthSpec` we can.

    Returns ``None`` only when the page has no login-like controls at
    all (no inputs, no SSO button, no magic-link/OTP phrasing) — in
    which case the caller should treat the login URL as mis-declared.
    For every other shape we still return a spec so the orchestrator
    can render a scaffold and log exactly what needs manual attention.
    """
    user, pwd, submit = detect_login_fields(page)

    # 1. Classic inline form — username + password + submit.
    if user and pwd and submit:
        return AuthSpec(
            login_url=login_url,
            auth_kind="form",
            username_selector=user,
            password_selector=pwd,
            submit_selector=submit,
            success_indicator_url_contains=success_url_marker,
            storage_state_path=storage_state_path,
        )

    # 2. Magic-link first — explicit phrasing trumps heuristics.
    magic_btn = _scan_phrase_button(page, _MAGIC_LINK_PHRASES)
    if magic_btn is not None and user:
        logger.info("auth_probe_magic_link_detected")
        return AuthSpec(
            login_url=login_url,
            auth_kind="magic_link",
            username_selector=user,
            continue_selector=magic_btn,
            requires_external_completion=True,
            success_indicator_url_contains=success_url_marker,
            storage_state_path=storage_state_path,
            notes=[
                "magic-link flow: runner fills email and clicks send; "
                "completing the login requires opening the email link"
            ],
        )

    # 3. OTP code flow.
    otp_btn = _scan_phrase_button(page, _OTP_PHRASES)
    if otp_btn is not None and user:
        logger.info("auth_probe_otp_detected")
        return AuthSpec(
            login_url=login_url,
            auth_kind="otp_code",
            username_selector=user,
            continue_selector=otp_btn,
            requires_external_completion=True,
            success_indicator_url_contains=success_url_marker,
            storage_state_path=storage_state_path,
            notes=[
                "otp flow: runner fills email and requests a code; "
                "the code must be supplied manually or via an inbox poller"
            ],
        )

    # 4. SSO provider buttons.
    sso = _scan_sso_button(page)
    if sso is not None:
        auth_kind, sso_selector = sso
        logger.info(
            "auth_probe_sso_detected",
            auth_kind=auth_kind,
            selector_strategy=sso_selector.strategy.value,
        )
        return AuthSpec(
            login_url=login_url,
            auth_kind=auth_kind,
            username_selector=user,  # Entra may auto-fill from this hint
            sso_button_selector=sso_selector,
            success_indicator_url_contains=success_url_marker,
            storage_state_path=storage_state_path,
        )

    # 5. Username-first — user + "Next"/"Continue" button, no password.
    continue_btn = _scan_phrase_button(page, _CONTINUE_PHRASES)
    if user and continue_btn is not None:
        logger.info("auth_probe_username_first_detected")
        return AuthSpec(
            login_url=login_url,
            auth_kind="username_first",
            username_selector=user,
            continue_selector=continue_btn,
            success_indicator_url_contains=success_url_marker,
            storage_state_path=storage_state_path,
            notes=["username-first: password field expected after Continue"],
        )

    # 6. Email-only form (no provider, no explicit magic/otp phrasing,
    #    single email input + submit).
    if _has_email_only_input(page) and user and submit:
        logger.info("auth_probe_email_only_detected")
        return AuthSpec(
            login_url=login_url,
            auth_kind="email_only",
            username_selector=user,
            submit_selector=submit,
            requires_external_completion=True,
            success_indicator_url_contains=success_url_marker,
            storage_state_path=storage_state_path,
            notes=[
                "email-only flow: form accepts an email and dispatches a link/code; "
                "completing the login requires an external step"
            ],
        )

    # 7. Anything else with *some* credential-ish element — keep a scaffold.
    if user or submit:
        return AuthSpec(
            login_url=login_url,
            auth_kind="unknown_auth",
            username_selector=user,
            submit_selector=submit,
            success_indicator_url_contains=success_url_marker,
            storage_state_path=storage_state_path,
            notes=["unrecognised login shape — manual scaffold only"],
        )

    return None
