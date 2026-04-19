"""Actually perform the login and capture Playwright ``storage_state``.

This runs *inside* ``autocoder generate`` so the user does not have to
invoke pytest to bootstrap authentication. Two flows are supported:

* ``auth_kind == "form"`` — fill the detected username/password/submit
  selectors on the app's own login page.
* ``auth_kind == "sso_microsoft"`` — click the provider button, follow
  the cross-origin redirect to ``login.microsoftonline.com``, and drive
  the Entra user-journey with the well-known MSAL selectors:
  ``input[name="loginfmt"]`` → ``#idSIButton9`` → ``input[name="passwd"]``
  → ``#idSIButton9`` → optional ``Stay signed in`` prompt.

Selectors on the Entra side are tenant-customisable; we only match the
*default* Microsoft markup. Tenants with heavy customisation may need
overrides (see ``AUTH_MSFT_*`` env vars below).

All credentials come from environment variables — never hard-coded,
never logged. We log only whether the value is present (``bool``).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable

from playwright.sync_api import Page, TimeoutError as PWTimeout

from autocoder import logger
from autocoder.config import Settings
from autocoder.extract.browser import goto_resilient, open_session
from autocoder.models import AuthSpec, SelectorStrategy, StableSelector


@dataclass
class AuthRunResult:
    ok: bool
    reason: str = ""
    final_url: str = ""
    elapsed_s: float = 0.0
    diagnostics: dict = field(default_factory=dict)


def _locate(page: Page, sel: StableSelector):
    """Resolve a :class:`StableSelector` against a live page.

    Mirrors the subset of the runtime resolver we need here — we do not
    want to pull in the self-healing stack for a one-shot login flow.
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


# Modes where a password on the **app's own** login form is required
# to even begin the flow. SSO modes are deliberately NOT in this set:
# the password (if any) is entered on the IdP's page, and many enterprise
# tenants don't use one at all (conditional access + device trust + MFA).
_APP_PASSWORD_MODES = {"form"}

# Modes that could *benefit* from a password if one is configured (we
# will fill it on the IdP side if the page asks), but can also complete
# interactively or via SSO cookies without one.
_SSO_MODES = {"sso_microsoft", "sso_generic"}


def _credentials(spec: AuthSpec) -> tuple[str | None, str | None, str]:
    """Return ``(username, password, reason)``.

    ``reason`` is ``"ok"`` when the env is sufficient for ``spec.auth_kind``.
    Otherwise it is a short machine-readable code the caller surfaces.

    Rules:

    * Every mode requires a username (``missing_username`` otherwise).
    * ``form``: also requires a password (``missing_password`` otherwise).
    * ``sso_microsoft`` / ``sso_generic``: password is **optional**. The
      runner will fill it on the IdP page only if the page presents a
      password field AND the env supplied one; if it does not, the
      flow waits for interactive completion (typical for MFA-enabled
      enterprise tenants).
    * Every other mode (``username_first``, ``email_only``,
      ``magic_link``, ``otp_code``, ``unknown_auth``): username only.
    """
    username = os.environ.get(spec.username_env, "").strip() or None
    password = os.environ.get(spec.password_env, "").strip() or None

    if not username:
        return None, None, "missing_username"
    if spec.auth_kind in _APP_PASSWORD_MODES and not password:
        return username, None, "missing_password"
    return username, password, "ok"


def _interactive_timeout_ms(settings: Settings) -> int:
    """How long the runner waits for the authenticated state to arrive.

    Headed runs deserve a generous window because the user may need to
    complete MFA (Authenticator push, number-matching, SMS). Headless
    runs get a shorter window because nothing is interactive anyway —
    if SSO cookies do not land a session inside 90s, they won't.
    """
    override = os.environ.get("AUTH_INTERACTIVE_TIMEOUT_MS", "").strip()
    if override.isdigit():
        return int(override)
    return 300_000 if not settings.browser.headless else 90_000


def _wait_success(
    page: Page,
    spec: AuthSpec,
    settings: Settings,
    timeout_ms: int = 90_000,
) -> bool:
    """Return ``True`` when the page looks authenticated.

    We accept any of:

    * the current URL contains the declared ``success_indicator_url_contains``;
    * the current URL contains ``settings.base_url`` and is no longer
      on ``login.microsoftonline.com`` (covers the SSO return hop);
    * the declared ``success_indicator_text`` is visible.
    """
    marker_url = spec.success_indicator_url_contains or settings.base_url or ""
    marker_text = spec.success_indicator_text or ""

    def _probe() -> bool:
        try:
            url = page.url or ""
        except Exception:
            url = ""
        if marker_url and marker_url in url and "login.microsoftonline.com" not in url:
            return True
        if marker_text:
            try:
                if page.get_by_text(marker_text, exact=False).first.is_visible():
                    return True
            except Exception:
                pass
        # Fallback: we left the provider domain *and* we are no longer on the
        # app's /login path. That is the common "landed on home" shape.
        if "login.microsoftonline.com" not in url and "/login" not in url:
            return bool(url and url != "about:blank")
        return False

    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        if _probe():
            return True
        try:
            page.wait_for_timeout(500)
        except Exception:
            time.sleep(0.5)
    return False


def _run_form_flow(page: Page, spec: AuthSpec, username: str, password: str) -> None:
    assert spec.username_selector is not None
    assert spec.password_selector is not None
    assert spec.submit_selector is not None
    _locate(page, spec.username_selector).fill(username)
    _locate(page, spec.password_selector).fill(password)
    _locate(page, spec.submit_selector).click()


def _run_email_only_flow(
    page: Page, spec: AuthSpec, username: str
) -> None:
    """Fill the email input and click submit (or the magic-link button)."""
    assert spec.username_selector is not None
    _locate(page, spec.username_selector).fill(username)
    submit = spec.continue_selector or spec.submit_selector
    if submit is not None:
        _locate(page, submit).click()


def _run_username_first_flow(
    page: Page,
    spec: AuthSpec,
    username: str,
    password: str | None,
    timeout_ms: int = 30_000,
) -> str:
    """Fill the username, click Next, then try to satisfy whatever comes next.

    Returns one of:

    * ``"password_completed"`` — password appeared after Next and we filled it.
    * ``"sso_chained"``         — after Next the page redirected to an IdP that
                                  we could not automate in this runner.
    * ``"awaiting_external"``   — after Next the page asked for a code or
                                  other non-password challenge.
    * ``"no_second_step"``      — we clicked Next but nothing changed.
    """
    assert spec.username_selector is not None
    _locate(page, spec.username_selector).fill(username)
    if spec.continue_selector is not None:
        _locate(page, spec.continue_selector).click()
    else:
        # Fall back to the generic submit selector.
        if spec.submit_selector is not None:
            _locate(page, spec.submit_selector).click()

    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        try:
            if page.locator('input[type="password"]').first.is_visible():
                if password:
                    page.locator('input[type="password"]').first.fill(password)
                    # Best-effort submit.
                    for sel in (
                        'button[type="submit"]',
                        'input[type="submit"]',
                        '#idSIButton9',
                    ):
                        try:
                            btn = page.locator(sel).first
                            if btn.count() > 0 and btn.is_visible():
                                btn.click(timeout=5_000)
                                return "password_completed"
                        except Exception:
                            continue
                    return "password_completed"
                return "awaiting_external"
        except Exception:
            pass
        try:
            url = page.url or ""
            if "login.microsoftonline.com" in url or "/oauth" in url:
                return "sso_chained"
        except Exception:
            pass
        try:
            page.wait_for_timeout(500)
        except Exception:
            time.sleep(0.5)
    return "no_second_step"


def _run_microsoft_sso_flow(
    context,
    page: Page,
    spec: AuthSpec,
    username: str,
    password: str | None,
) -> Page:
    """Click the provider button and drive the Entra page.

    Returns the :class:`Page` we should observe for success (the SSO
    flow may happen in the original tab *or* in a popup; we handle both).

    Password handling:

    * If ``password`` is provided and the Entra page renders a
      password input, we fill + submit it.
    * If ``password`` is ``None`` **or** the password input never
      appears within the short wait window, we leave the page where
      it is — the MFA / number-match / passwordless screen the user
      sees will be completed interactively (headed mode) or will
      time out cleanly in ``_wait_success`` (headless mode).

    The previous implementation raised on a missing password field;
    that broke passwordless / MFA-first tenants. Now we treat a
    missing password input as a signal that we should hand control to
    the user rather than as a fatal error.
    """
    assert spec.sso_button_selector is not None

    # Some MSAL apps open the IdP in a popup; some redirect in-place.
    popup_page: Page | None = None
    popup_ctx: dict[str, Page | None] = {"p": None}

    def _on_popup(p: Page) -> None:
        popup_ctx["p"] = p

    context.on("page", _on_popup)
    _locate(page, spec.sso_button_selector).click()

    # Give the popup a beat to appear. If it does, switch to it.
    try:
        page.wait_for_timeout(1500)
    except Exception:
        pass
    popup_page = popup_ctx["p"]
    active: Page = popup_page if popup_page is not None else page

    # Wait for the Entra page to mount. Accept either the email input
    # or the "Pick an account" tile screen.
    entra_host = "login.microsoftonline.com"
    deadline = time.monotonic() + 45.0
    while time.monotonic() < deadline:
        url = active.url or ""
        if entra_host in url:
            break
        # The popup may have replaced itself with a fresh navigation.
        popup_page = popup_ctx["p"] or popup_page
        if popup_page is not None and entra_host in (popup_page.url or ""):
            active = popup_page
            break
        try:
            active.wait_for_timeout(500)
        except Exception:
            time.sleep(0.5)

    # Account-tile screen: if our username is already present, click it.
    try:
        tile = active.get_by_role("link", name=username)
        if tile.count() > 0 and tile.first.is_visible():
            tile.first.click()
    except Exception:
        pass

    # Email/UPN input.
    email_css = os.environ.get(
        "AUTH_MSFT_EMAIL_SELECTOR", 'input[name="loginfmt"], input[type="email"]'
    )
    try:
        active.locator(email_css).first.fill(username, timeout=30_000)
    except PWTimeout:
        # Might already have jumped past email (seen when browser has
        # Entra cookies for the tenant). Fall through to password.
        pass
    else:
        # Click "Next".
        next_css = os.environ.get(
            "AUTH_MSFT_NEXT_SELECTOR",
            '#idSIButton9, input[type="submit"], button[type="submit"]',
        )
        try:
            active.locator(next_css).first.click(timeout=10_000)
        except Exception:
            pass

    # Password field — best-effort. If the field is absent or we do
    # not have a password configured, we simply hand off to
    # ``_wait_success`` which gives the user a long window to finish
    # the flow interactively (MFA, passwordless, SMS, number-match).
    pwd_css = os.environ.get(
        "AUTH_MSFT_PASSWORD_SELECTOR", 'input[name="passwd"], input[type="password"]'
    )
    pwd_appeared = False
    try:
        active.locator(pwd_css).first.wait_for(state="visible", timeout=15_000)
        pwd_appeared = True
    except PWTimeout:
        logger.info(
            "auth_sso_password_input_absent",
            hint=(
                "No password input on the Entra page within 15s — tenant "
                "may be passwordless / MFA-first. Waiting for interactive "
                "completion."
            ),
        )

    if pwd_appeared and password:
        try:
            active.locator(pwd_css).first.fill(password, timeout=10_000)
        except Exception:
            pass
    elif pwd_appeared and not password:
        logger.info(
            "auth_sso_password_requested_but_absent",
            hint=(
                "Entra requested a password but LOGIN_PASSWORD is not set. "
                "User must complete entry interactively (HEADLESS=false)."
            ),
        )

    # Only click the post-password submit when we actually filled a
    # password. Otherwise we risk skipping past an MFA / passwordless
    # prompt the user is about to interact with.
    if pwd_appeared and password:
        submit_css = os.environ.get(
            "AUTH_MSFT_SUBMIT_SELECTOR",
            '#idSIButton9, input[type="submit"], button[type="submit"]',
        )
        try:
            active.locator(submit_css).first.click(timeout=10_000)
        except Exception:
            pass

    # Optional "Stay signed in?" prompt.
    kmsi_css = os.environ.get("AUTH_MSFT_KMSI_SELECTOR", "#idSIButton9, #acceptButton")
    try:
        active.locator(kmsi_css).first.click(timeout=8_000)
    except Exception:
        pass

    return active


def run_auth(
    spec: AuthSpec,
    settings: Settings,
    *,
    on_storage_saved: Callable[[str], None] | None = None,
) -> AuthRunResult:
    """Perform login end-to-end and write storage_state on success.

    The dispatch is driven by ``spec.auth_kind`` rather than a fixed
    "username + password must be present" check. Modes that cannot be
    completed by this runner (magic link, OTP, unknown_auth) still
    reach the page, fill whatever the user supplied, capture whatever
    cookies the IdP dropped so far, and return a clear ``reason``
    that tells the orchestrator the flow needs external completion.
    """
    username, password, creds_reason = _credentials(spec)
    if creds_reason == "missing_username":
        return AuthRunResult(
            ok=False,
            reason="missing_credentials",
            diagnostics={
                "auth_kind": spec.auth_kind,
                "username_env": spec.username_env,
                "password_env": spec.password_env,
                "username_present": False,
                "password_present": bool(password),
            },
        )
    if creds_reason == "missing_password":
        return AuthRunResult(
            ok=False,
            reason="missing_password_for_password_mode",
            diagnostics={
                "auth_kind": spec.auth_kind,
                "hint": (
                    "mode needs a password on the app's own login form; "
                    "set LOGIN_PASSWORD or adjust the AuthSpec if this "
                    "was mis-detected"
                ),
            },
        )

    storage_path = settings.paths.storage_state
    storage_path.parent.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    interactive_ms = _interactive_timeout_ms(settings)
    is_sso = spec.auth_kind in _SSO_MODES
    logger.info(
        "auth_runner_start",
        login_url=logger.safe_url(spec.login_url),
        auth_kind=spec.auth_kind,
        username_present=bool(username),
        password_present=bool(password),
        app_password_required=spec.auth_kind in _APP_PASSWORD_MODES,
        interactive_timeout_ms=interactive_ms,
        headless=settings.browser.headless,
        mode_supports_interactive=is_sso,
    )
    if is_sso and settings.browser.headless:
        logger.warn(
            "auth_sso_headless",
            hint=(
                "SSO detected but HEADLESS=true. Enterprise Entra tenants "
                "usually require MFA — set HEADLESS=false for the first "
                "capture so you can complete the prompt interactively."
            ),
        )

    try:
        with open_session(settings, use_storage_state=False) as sess:
            try:
                diag = goto_resilient(
                    sess.page,
                    spec.login_url,
                    nav_timeout_ms=settings.browser.extraction_nav_timeout_ms,
                    diagnostics_dir=settings.paths.logs_dir,
                )
            except Exception as exc:
                return AuthRunResult(
                    ok=False,
                    reason="login_page_unreachable",
                    elapsed_s=time.monotonic() - started,
                    diagnostics={"err": str(exc)},
                )

            awaiting_external = False
            second_step: str | None = None
            active = sess.page

            if spec.auth_kind == "form":
                _run_form_flow(sess.page, spec, username, password or "")
            elif spec.auth_kind == "sso_microsoft":
                active = _run_microsoft_sso_flow(
                    sess.context, sess.page, spec, username, password or ""
                )
            elif spec.auth_kind == "sso_generic":
                if spec.sso_button_selector is None:
                    return AuthRunResult(
                        ok=False,
                        reason="sso_generic_no_button",
                        elapsed_s=time.monotonic() - started,
                    )
                _locate(sess.page, spec.sso_button_selector).click()
                # Best-effort credential fill. Any step that fails is
                # treated as "the user is about to interact with this";
                # we fall through to _wait_success with the interactive
                # timeout so MFA / passwordless flows have room to breathe.
                try:
                    active.wait_for_selector(
                        'input[type="password"], input[type="email"]',
                        timeout=15_000,
                    )
                except Exception:
                    pass
                try:
                    if spec.username_selector:
                        _locate(active, spec.username_selector).fill(username)
                    else:
                        active.locator(
                            'input[type="email"], input[name*="user" i]'
                        ).first.fill(username, timeout=5_000)
                except Exception:
                    pass
                if password:
                    try:
                        active.locator('input[type="password"]').first.fill(
                            password, timeout=5_000
                        )
                        btn = active.locator('button[type="submit"], input[type="submit"]')
                        if btn.count() > 0:
                            btn.first.click(timeout=5_000)
                    except Exception:
                        pass
            elif spec.auth_kind == "username_first":
                second_step = _run_username_first_flow(
                    sess.page, spec, username, password
                )
                awaiting_external = second_step in {
                    "awaiting_external",
                    "sso_chained",
                    "no_second_step",
                }
            elif spec.auth_kind in ("email_only", "magic_link", "otp_code"):
                _run_email_only_flow(sess.page, spec, username)
                awaiting_external = True
            else:  # unknown_auth
                awaiting_external = True

            if awaiting_external:
                # We did what we could. Persist cookies the provider has
                # already set (so any further session restoration has a
                # head start) and surface the external-completion flag.
                try:
                    sess.context.storage_state(path=str(storage_path))
                    if on_storage_saved is not None:
                        try:
                            on_storage_saved(str(storage_path))
                        except Exception:
                            pass
                except Exception:
                    pass
                return AuthRunResult(
                    ok=False,
                    reason="awaiting_external_completion",
                    final_url=active.url,
                    elapsed_s=time.monotonic() - started,
                    diagnostics={
                        "auth_kind": spec.auth_kind,
                        "second_step": second_step,
                        "hint": (
                            "runner progressed as far as it can; finish the "
                            "login manually (magic link / OTP / MFA) then "
                            "rerun — the storage file was still written so "
                            "any provider cookies are preserved"
                        ),
                        "nav": diag.to_dict(),
                    },
                )

            wait_timeout_ms = interactive_ms if is_sso else 90_000
            if is_sso:
                logger.info(
                    "auth_awaiting_success",
                    auth_kind=spec.auth_kind,
                    timeout_ms=wait_timeout_ms,
                    hint=(
                        "waiting for post-login URL signal; complete MFA in "
                        "the visible browser window if prompted"
                    ),
                )
            if not _wait_success(active, spec, settings, timeout_ms=wait_timeout_ms):
                _dump_failure_artifacts(active, settings, spec)
                return AuthRunResult(
                    ok=False,
                    reason="success_indicator_not_seen",
                    final_url=active.url,
                    elapsed_s=time.monotonic() - started,
                    diagnostics={
                        "auth_kind": spec.auth_kind,
                        "waited_ms": wait_timeout_ms,
                        "hint": (
                            "never left login.microsoftonline.com / /login "
                            "within the timeout. Usual causes: MFA not "
                            "completed, wrong tenant, conditional-access "
                            "block. Run again with HEADLESS=false and "
                            "complete the prompt when it appears."
                        ),
                        "nav": diag.to_dict(),
                    },
                )

            sess.context.storage_state(path=str(storage_path))
            if on_storage_saved is not None:
                try:
                    on_storage_saved(str(storage_path))
                except Exception:
                    pass
            return AuthRunResult(
                ok=True,
                reason="ok",
                final_url=active.url,
                elapsed_s=time.monotonic() - started,
                diagnostics={"nav": diag.to_dict()},
            )
    except Exception as exc:  # noqa: BLE001
        return AuthRunResult(
            ok=False,
            reason="auth_runner_exception",
            elapsed_s=time.monotonic() - started,
            diagnostics={"err": str(exc)},
        )


def _dump_failure_artifacts(page: Page, settings: Settings, spec: AuthSpec) -> None:
    """Screenshot + HTML on auth failure so the user can diagnose offline."""
    try:
        out = settings.paths.logs_dir
        out.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = out / f"auth_failure_{stamp}_{spec.auth_kind}"
        try:
            page.screenshot(path=str(base) + ".png", full_page=True)
        except Exception:
            pass
        try:
            (base.parent / f"{base.name}.html").write_text(
                page.content() or "", encoding="utf-8"
            )
        except Exception:
            pass
        logger.warn("auth_failure_artifacts", base=str(base))
    except Exception:
        pass
