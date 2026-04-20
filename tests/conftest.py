"""Pytest fixtures for the generated test suite.

Two distinct ``page`` fixtures are exposed:

* ``page``      — uses ``storage_state`` if it exists, so the test
                  starts authenticated. This is the default.
* ``raw_page``  — fresh context with no storage_state. Use it for
                  the auth-setup test or any explicitly-anonymous
                  scenario.

The module also bridges two env vars from ``.env`` into the
``pytest-playwright`` plugin so the autocoder side and the pytest
side stay in sync:

* ``HEADLESS``                 -> browser launch headed/headless mode
* ``PW_SLOWMO_MS``             -> per-action slow-motion delay in ms
                                  (useful when ``HEADLESS=false`` so
                                  you can watch the flow)

Priority: an explicit ``pytest --headed`` / ``--headless`` flag
always wins over the env var. This lets CI override ``.env``
without editing files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

import pytest
from playwright.sync_api import Browser, BrowserContext, Page


# Auto-heal plugin: no-op unless ``AUTOCODER_AUTOHEAL=true``. When
# enabled, it collects failures during the session, patches the
# offending step bodies via the same local LLM used by
# ``autocoder heal``, and (optionally) re-runs the failed tests.
pytest_plugins = ["tests.support.autoheal_plugin"]


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        return int(raw)
    except ValueError:
        return default


def _storage_state_path() -> Path:
    raw = os.environ.get("STORAGE_STATE", ".auth/user.json")
    p = Path(raw)
    if not p.is_absolute():
        p = Path.cwd() / p
    return p


def _session_storage_path() -> Path:
    """Companion file that holds a snapshot of ``window.sessionStorage``.

    ``autocoder run`` / ``auth_runner.run_auth`` writes it alongside
    ``storage_state`` so every test context can replay MSAL's
    authenticated account — which lives in sessionStorage and is NOT
    persisted by Playwright's ``storage_state`` by design.
    """
    sp = _storage_state_path()
    return sp.with_name(sp.stem + ".session_storage" + sp.suffix)


def _load_session_storage_snapshot() -> dict[str, str]:
    p = _session_storage_path()
    if not p.exists() or p.stat().st_size == 0:
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _autoauth_enabled() -> bool:
    """True when pytest should trigger auth itself if the session is missing.

    Default: **on** whenever ``LOGIN_URL`` is set in ``.env``. Turn
    off with ``AUTOCODER_AUTOAUTH=false`` for headless CI runs where
    the captured session is shipped via a secret manager and no
    interactive login is possible.
    """
    raw = os.environ.get("AUTOCODER_AUTOAUTH", "").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    if raw in {"1", "true", "yes", "on"}:
        return True
    return bool(os.environ.get("LOGIN_URL", "").strip())


@pytest.fixture(scope="session", autouse=True)
def _ensure_auth_session() -> None:
    """Guarantee a valid ``.auth/user.json`` before any test runs.

    If the file is missing (first-time use, expired session deleted,
    or freshly-cloned checkout) **and** autoauth is enabled, this
    opens a Chromium window via ``autocoder.extract.auth_runner``,
    drives the detected login shape (form / SSO / username-first /
    email-only), and captures both ``storage_state`` and the
    ``session_storage`` companion. The popup blocks until you
    complete MFA in the window.

    Whenever the files already exist, this is a no-op. The freshness
    check is intentionally lightweight (existence only) — an expired
    session shows up as a test failure later and the autoheal
    plugin + the normal ``autocoder run --force`` flow take it from
    there.
    """
    sp = _storage_state_path()
    if sp.exists() and sp.stat().st_size > 0:
        return
    if not _autoauth_enabled():
        return

    # Import lazily so a checkout without the autocoder package
    # installed (e.g. a downstream user running only the generated
    # tests) still collects this conftest cleanly.
    try:
        from autocoder.config import load_settings
        from autocoder.extract.auth_probe import build_auth_spec
        from autocoder.extract.auth_runner import run_auth
        from autocoder.extract.browser import goto_resilient, open_shared_session
        from autocoder.models import Status
    except Exception as exc:  # noqa: BLE001
        pytest.skip(
            f"autoauth requested but autocoder package is not importable: {exc!s}"
        )
        return

    settings = load_settings()
    if not settings.login_url:
        pytest.skip(
            "autoauth: no .auth/user.json and LOGIN_URL is not set in .env"
        )
        return

    with open_shared_session(settings, use_storage_state=False) as shared:
        try:
            goto_resilient(
                shared.page,
                settings.login_url,
                nav_timeout_ms=settings.browser.extraction_nav_timeout_ms,
                diagnostics_dir=settings.paths.logs_dir,
            )
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"autoauth: could not reach LOGIN_URL: {exc!s}")
            return
        spec = build_auth_spec(
            shared.page,
            login_url=settings.login_url,
            storage_state_path=str(sp),
            success_url_marker=settings.base_url or None,
        )
        if spec is None:
            pytest.skip(
                "autoauth: could not detect a login form or SSO button at "
                f"{settings.login_url}"
            )
            return
        spec = spec.model_copy(update={"status": Status.STEPS_READY})
        result = run_auth(spec, settings, shared=shared)
        if not result.ok:
            pytest.skip(
                f"autoauth: login flow did not capture a session "
                f"(reason={result.reason})"
            )
            return


def _inject_session_storage(context: BrowserContext) -> None:
    """Replay captured sessionStorage on every page in ``context``.

    Uses ``BrowserContext.add_init_script``: every new page (and
    every sub-frame) evaluates the script before any of its own
    scripts run. By the time MSAL.js boots and checks its storage,
    the captured account keys are already there.

    Safe to call even when the companion file is empty or absent —
    the generated script becomes a no-op.
    """
    snapshot = _load_session_storage_snapshot()
    if not snapshot:
        return
    # Serialize via ``json.dumps`` so quotes and control chars are
    # handled correctly. No string interpolation of user data into JS.
    payload = json.dumps(snapshot)
    script = (
        "(() => {"
        "  try {"
        f"    const e = {payload};"
        "    for (const k of Object.keys(e)) {"
        "      try { window.sessionStorage.setItem(k, e[k]); } catch (err) {}"
        "    }"
        "  } catch (err) {}"
        "})();"
    )
    try:
        context.add_init_script(script)
    except Exception:
        pass


@pytest.fixture(scope="session")
def base_url() -> str:
    return os.environ.get("BASE_URL", "").rstrip("/")


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):  # noqa: F811
    """Bridge ``HEADLESS`` and ``PW_SLOWMO_MS`` from .env into pytest-playwright.

    By default ``pytest-playwright`` only honours its own ``--headed``
    flag — it does **not** read the ``HEADLESS`` env var. That's why
    ``pytest tests/playwright`` is silent even when ``.env`` has
    ``HEADLESS=false``. This fixture closes that gap.

    An explicit ``--headed`` / ``--headless`` CLI flag still wins,
    because those flags mutate ``browser_type_launch_args`` before
    this fixture runs.
    """
    args = dict(browser_type_launch_args)
    # Respect CLI overrides: if the key was set by --headed/--headless
    # handling upstream, leave it alone.
    if "headless" not in browser_type_launch_args:
        args["headless"] = _env_flag("HEADLESS", True)
    slowmo = _env_int("PW_SLOWMO_MS", 0)
    if slowmo > 0 and "slow_mo" not in browser_type_launch_args:
        args["slow_mo"] = slowmo
    return args


@pytest.fixture
def browser_context_args(browser_context_args, base_url):  # noqa: F811
    storage_state = _storage_state_path()
    args = dict(browser_context_args)
    if base_url:
        args["base_url"] = base_url
    if storage_state.exists() and storage_state.stat().st_size > 0:
        args["storage_state"] = str(storage_state)
    return args


@pytest.fixture
def context(browser: Browser, browser_context_args) -> Iterator[BrowserContext]:
    """Test-scoped browser context with MSAL sessionStorage pre-loaded.

    Overrides the ``pytest-playwright`` default so every generated
    test inherits whatever auth state ``autocoder run`` captured:

    * cookies + localStorage come from ``storage_state`` (already
      wired by our ``browser_context_args`` override above);
    * ``sessionStorage`` — which Playwright does not persist and
      which MSAL.js depends on — is replayed via
      :func:`_inject_session_storage`.

    The init script runs before any page script, so the SPA finds
    MSAL's authenticated account on mount and renders the real app
    instead of the pre-auth consent shell — whether the test is
    invoked on its own or as part of a larger run.
    """
    ctx = browser.new_context(**browser_context_args)
    _inject_session_storage(ctx)
    try:
        yield ctx
    finally:
        ctx.close()


@pytest.fixture
def raw_context(browser: Browser, base_url) -> Iterator[BrowserContext]:
    """Fresh context, ignores any saved storage_state."""
    kwargs: dict = {}
    if base_url:
        kwargs["base_url"] = base_url
    context = browser.new_context(**kwargs)
    try:
        yield context
    finally:
        context.close()


@pytest.fixture
def raw_page(raw_context: BrowserContext) -> Iterator[Page]:
    page = raw_context.new_page()
    try:
        yield page
    finally:
        page.close()
