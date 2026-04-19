"""Classify provided URLs by probing them in a real browser.

We open each URL anonymously (no storage_state). The classifier looks
for three signals:

1. **Did the request redirect to a login URL?** -> ``REDIRECT_TO_LOGIN``,
   ``requires_auth=True``, depends on the discovered login URL.
2. **Does the page expose a credential form?** -> ``LOGIN``.
3. **Does the page render content with no login form?** -> ``PUBLIC``.

This is the *only* place login-URL discovery happens automatically —
the rest of the system trusts ``Registry.auth.login_url`` once the
classifier writes it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlparse

from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

from autocoder import logger
from autocoder.config import Settings
from autocoder.extract.browser import AuthUnreachable, goto_resilient
from autocoder.models import URLKind, URLNode
from autocoder.utils import url_slug


_LOGIN_URL_HINTS = (
    "login",
    "signin",
    "sign-in",
    "sso",
    "auth",
    "account/login",
    "oauth",
    "openid",
)
_LOGIN_INPUT_NAME_HINTS = ("email", "user", "username", "login", "account")
_LOGIN_PASSWORD_TYPE = 'input[type="password"]'


def looks_like_login_url(url: str) -> bool:
    """Public URL-path heuristic.

    Exposed so the orchestrator can make auth-first decisions when
    classification could not reach the page (timeouts, SSO redirects).
    """
    if not url:
        return False
    p = urlparse(url).path.lower()
    return any(hint in p for hint in _LOGIN_URL_HINTS)


# Back-compat alias so older imports keep working.
_looks_like_login_url = looks_like_login_url


def _looks_like_login_page(page: Page) -> bool:
    """Heuristic: a password field plus a username-ish field."""
    try:
        if page.locator(_LOGIN_PASSWORD_TYPE).count() == 0:
            return False
    except Exception:
        return False
    for hint in _LOGIN_INPUT_NAME_HINTS:
        try:
            if page.locator(f"input[name*={hint} i], input[id*={hint} i]").count() > 0:
                return True
        except Exception:
            continue
    return True  # password field alone is enough signal


# Phrases that, when present on an anonymously-reachable page, mean
# "the real app is behind this prompt; do not treat what you see as
# the authenticated experience". These are deliberately conservative
# — common CTAs on truly public marketing pages ("Sign up", "Try
# free") are NOT in the list.
_AUTH_GATED_PHRASES: tuple[str, ...] = (
    "sign in with microsoft",
    "continue with microsoft",
    "login with microsoft",
    "sign in with google",
    "continue with google",
    "sign in with github",
    "continue with github",
    "sign in with sso",
    "single sign-on",
    "sign in to continue",
    "please sign in",
    "you must sign in",
    "login required",
    "authentication required",
)


def _looks_auth_gated(page: Page) -> bool:
    """Return True when an anonymously-loaded page advertises an auth wall.

    Two signals, either is sufficient:

    1. A visible provider button whose accessible name matches a
       known SSO phrase (Microsoft / Google / GitHub / generic SSO).
    2. Prominent "Sign in to continue" / "Authentication required"
       text on the page.

    We use this in addition to the existing password-field / redirect
    checks so that:

    * A "shell" page that renders only an SSO button (and maybe a
      terms-of-service checkbox) is flagged as auth-gated instead of
      ``PUBLIC``.
    * The orchestrator then knows to run auth-first and re-extract
      the URL under storage_state after the session is captured.
    """
    import re as _re

    for phrase in _AUTH_GATED_PHRASES:
        # Role-name matches are the most reliable and also the cheapest.
        regex = _re.compile(_re.escape(phrase), _re.IGNORECASE)
        for role in ("button", "link"):
            try:
                loc = page.get_by_role(role, name=regex)
                if loc.count() > 0:
                    h = loc.first.element_handle()
                    if h and h.is_visible():
                        return True
            except Exception:
                continue
        # Fallback: any visible text match. Only trigger when the
        # phrase appears as a distinct element, not inside a policy
        # blob, by checking for a tag-scoped text match.
        try:
            txt = page.get_by_text(regex)
            if txt.count() > 0:
                h = txt.first.element_handle()
                if h and h.is_visible():
                    return True
        except Exception:
            continue
    return False


def classify_urls(
    urls: Iterable[str],
    settings: Settings,
) -> tuple[list[URLNode], str | None]:
    """Probe each URL once. Returns (nodes, detected_login_url).

    ``detected_login_url`` is filled in when one of the probes lands on,
    or is redirected to, a page that looks like a login form. Callers
    use it to seed ``Registry.auth.login_url`` when no LOGIN_URL was
    provided in the env.
    """
    urls = [u for u in urls if u]
    if not urls:
        return [], None

    nodes: list[URLNode] = []
    detected_login_url: str | None = None
    seen_slugs: dict[str, int] = {}

    nav_timeout = settings.browser.extraction_nav_timeout_ms
    logger.info(
        "classify_start",
        count=len(urls),
        headless=settings.browser.headless,
        nav_timeout_ms=nav_timeout,
    )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=settings.browser.headless)
        context = browser.new_context()
        page = context.new_page()

        for raw_url in urls:
            slug = url_slug(raw_url)
            seen_slugs[slug] = seen_slugs.get(slug, 0) + 1
            if seen_slugs[slug] > 1:
                slug = f"{slug}_{seen_slugs[slug]}"

            node = URLNode(url=raw_url, slug=slug)

            # Pre-mark LOGIN from URL path so the signal survives any
            # navigation failure. A real form check below can only
            # upgrade, never downgrade, this early guess.
            url_is_login_shaped = looks_like_login_url(raw_url)
            if url_is_login_shaped:
                node.kind = URLKind.LOGIN
                detected_login_url = detected_login_url or raw_url
                node.notes.append("login_inferred_from_path")

            try:
                diag = goto_resilient(
                    page,
                    raw_url,
                    nav_timeout_ms=nav_timeout,
                    diagnostics_dir=settings.paths.logs_dir,
                )
                final_url = diag.final_url or page.url
                node.redirects_to = final_url if final_url != raw_url else None
                status = diag.status

                redirected_to_login = (
                    node.redirects_to is not None and looks_like_login_url(final_url)
                )
                page_is_login = _looks_like_login_page(page)

                reason = ""
                if page_is_login and (raw_url == final_url or looks_like_login_url(raw_url)):
                    node.kind = URLKind.LOGIN
                    detected_login_url = detected_login_url or final_url
                    reason = "password_field_present_at_target"
                elif redirected_to_login:
                    node.kind = URLKind.REDIRECT_TO_LOGIN
                    node.requires_auth = True
                    detected_login_url = detected_login_url or final_url
                    reason = "redirected_to_login_shaped_url"
                elif page_is_login:
                    node.kind = URLKind.LOGIN
                    detected_login_url = detected_login_url or final_url
                    reason = "password_field_present_post_redirect"
                elif url_is_login_shaped:
                    # URL path *said* it is a login page even though we
                    # could not find a password field — keep the hint.
                    node.kind = URLKind.LOGIN
                    reason = "login_path_hint_preserved"
                else:
                    node.kind = URLKind.PUBLIC
                    reason = "no_password_field_no_login_redirect"

                # Whatever we decided above, also check for an
                # auth-wall affordance (SSO button, "Sign in to
                # continue" prompt). When present on a page that is
                # not already a LOGIN page, mark it as requiring an
                # authenticated session: the anonymous DOM we captured
                # is the pre-auth shell, not the real application.
                if node.kind == URLKind.PUBLIC and _looks_auth_gated(page):
                    node.requires_auth = True
                    node.notes.append("auth_gated_shell_detected")
                    reason = f"{reason}+auth_gated_shell"
                    logger.info(
                        "classify_auth_gated_shell",
                        slug=slug,
                        url=logger.safe_url(raw_url),
                        hint=(
                            "page has a visible SSO / sign-in affordance; "
                            "re-extraction under storage_state will capture "
                            "the authenticated view"
                        ),
                    )

                if status is not None and status >= 400:
                    node.notes.append(f"http_status={status}")
                    logger.warn(
                        "classify_http_error",
                        url=logger.safe_url(raw_url),
                        status=status,
                    )
                logger.info(
                    "classify",
                    slug=slug,
                    url=logger.safe_url(raw_url),
                    final=logger.safe_url(final_url),
                    kind=node.kind.value,
                    requires_auth=node.requires_auth,
                    reason=reason,
                    wait_strategy=diag.wait_strategy,
                    http=(status if status is not None else 0),
                )
            except AuthUnreachable as exc:
                # Navigation itself timed out. Decide from path hints
                # and explicit LOGIN_URL config rather than silently
                # dropping the URL into UNKNOWN.
                node.notes.append(f"nav_timeout: {exc!s}")
                if url_is_login_shaped:
                    node.kind = URLKind.LOGIN
                    detected_login_url = detected_login_url or raw_url
                    logger.warn(
                        "classify_timeout_login_inferred",
                        slug=slug,
                        url=logger.safe_url(raw_url),
                        nav_timeout_ms=nav_timeout,
                        **exc.diag.to_dict(),
                    )
                elif settings.login_url:
                    # An auth-protected URL that cannot even commit,
                    # while the user has declared a login endpoint.
                    # Mark it as needing auth so auth-first can fire.
                    node.kind = URLKind.UNKNOWN
                    node.requires_auth = True
                    node.notes.append("unreachable_marking_requires_auth")
                    logger.warn(
                        "classify_timeout_escalated_to_auth",
                        slug=slug,
                        url=logger.safe_url(raw_url),
                        login_url=logger.safe_url(settings.login_url),
                        **exc.diag.to_dict(),
                    )
                else:
                    node.kind = URLKind.UNKNOWN
                    logger.warn(
                        "classify_timeout",
                        slug=slug,
                        url=logger.safe_url(raw_url),
                        nav_timeout_ms=nav_timeout,
                        **exc.diag.to_dict(),
                    )
            except PWTimeout as exc:
                # Defensive: goto_resilient normalises to AuthUnreachable,
                # but downstream waits could still surface the raw error.
                node.notes.append(f"timeout: {exc!s}")
                if url_is_login_shaped:
                    node.kind = URLKind.LOGIN
                    detected_login_url = detected_login_url or raw_url
                else:
                    node.kind = URLKind.UNKNOWN
                    if settings.login_url:
                        node.requires_auth = True
                        node.notes.append("unreachable_marking_requires_auth")
                logger.warn(
                    "classify_timeout",
                    slug=slug,
                    url=logger.safe_url(raw_url),
                    nav_timeout_ms=nav_timeout,
                )
            except Exception as exc:  # noqa: BLE001
                node.notes.append(f"error: {exc!s}")
                if url_is_login_shaped:
                    node.kind = URLKind.LOGIN
                    detected_login_url = detected_login_url or raw_url
                else:
                    node.kind = URLKind.UNKNOWN
                logger.warn("classify_error", slug=slug, url=logger.safe_url(raw_url), err=str(exc))

            nodes.append(node)

        context.close()
        browser.close()

    counts = {k.value: 0 for k in URLKind}
    for n in nodes:
        counts[n.kind.value] = counts.get(n.kind.value, 0) + 1
    logger.ok(
        "classify_done",
        nodes=len(nodes),
        login_detected=bool(detected_login_url),
        **{f"kind_{k}": v for k, v in counts.items() if v},
    )
    return nodes, detected_login_url


_REGION_RE = re.compile(r"https?://[^/]+", re.IGNORECASE)


def same_origin(a: str, b: str) -> bool:
    am = _REGION_RE.match(a)
    bm = _REGION_RE.match(b)
    return bool(am and bm and am.group(0).lower() == bm.group(0).lower())
