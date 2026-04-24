"""Pytest fixtures for the generated test suite.

Two distinct ``page`` fixtures are exposed:

* ``page``      — uses ``storage_state`` if it exists, so the test
                  starts authenticated. This is the default.
* ``raw_page``  — fresh context with no storage_state. Use it for
                  the auth-setup test or any explicitly-anonymous
                  scenario.

All environment-driven knobs (``HEADLESS``, ``PW_SLOWMO_MS``,
``STORAGE_STATE``, ``LOGIN_URL``, autoheal/autoauth toggles, …) are
read from :mod:`tests.settings`, **not** from ``os.environ``
directly. That keeps ``.env`` a single-source-of-truth file; this
module never touches the process env.

Priority for the headed/headless flag: an explicit
``pytest --headed`` / ``--headless`` CLI flag always wins over the
env var. This lets CI override ``.env`` without editing files.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Browser, BrowserContext, Page

from tests import settings


# Auto-heal plugin: no-op unless ``AUTOCODER_AUTOHEAL=true``. When
# enabled, it collects failures during the session, patches the
# offending step bodies via the same local LLM used by
# ``autocoder heal``, and (optionally) re-runs the failed tests.
pytest_plugins = ["tests.support.autoheal_plugin"]


def _load_session_storage_snapshot() -> dict[str, str]:
    p = settings.SESSION_STORAGE_COMPANION
    if not p.exists() or p.stat().st_size == 0:
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


# Cookies we treat as "session proof". At least one of these (or a
# Microsoft-issued ``ESTSAUTH*``) must be present and unexpired for
# ``_session_live()`` to return True.
_AUTH_COOKIE_TOKENS = ("estsauth", "estsauthpersistent", "idtoken", "ai_session", "appservice")


def _app_host() -> str:
    try:
        return urlparse(settings.BASE_URL).hostname or ""
    except Exception:
        return ""


def _session_live(grace_seconds: int = 300) -> bool:
    """Cheap liveness check for the captured ``storage_state``.

    Instead of doing an HTTP probe (which costs a round-trip and can
    time out), we parse the cookie expiry timestamps from
    ``.auth/user.json`` and verify that at least one auth-shaped
    cookie for the app domain or ``login.microsoftonline.com`` is
    still valid (with ``grace_seconds`` of slack — default 5 min).

    Returns ``True`` when the session looks usable. Returns ``False``
    — and triggers re-auth — when:

    * the file is missing or empty;
    * no cookies are present;
    * every auth cookie's ``expires`` is in the past (or within the
      grace window);
    * the JSON shape is unreadable.

    **Session cookies with no expiry (value = -1) are treated as
    valid** — Playwright writes them when the IdP issued a cookie
    without an explicit max-age. They are cleared when the browser
    process restarts, which for us is every run.
    """
    if not settings.storage_state_ready():
        return False
    try:
        raw = json.loads(settings.STORAGE_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False

    cookies = raw.get("cookies") or []
    if not cookies:
        return False

    now = time.time() + grace_seconds
    app_host = _app_host()

    def _is_relevant(c: dict) -> bool:
        domain = (c.get("domain") or "").lower()
        name = (c.get("name") or "").lower()
        return (
            ("microsoftonline.com" in domain)
            or (app_host and app_host in domain)
            or any(tok in name for tok in _AUTH_COOKIE_TOKENS)
        )

    relevant = [c for c in cookies if _is_relevant(c)]
    if not relevant:
        return False

    session_cookies = [c for c in relevant if c.get("expires", -1) in (None, -1, 0)]
    if session_cookies:
        # Session-scoped cookies from Entra count — they're only
        # invalidated when the browser process exits, which only
        # happens between runs.
        return True

    # All relevant cookies have explicit expiry; check the max.
    max_expiry = max(c.get("expires", 0) for c in relevant)
    return max_expiry > now


@pytest.fixture(scope="session", autouse=True)
def _ensure_auth_session() -> None:
    """Guarantee a live ``.auth/user.json`` before any test runs.

    First-time use, expired-session deleted, or freshly-cloned
    checkout — all paths converge here. The fixture:

    1. Checks that ``.auth/user.json`` exists and is non-empty.
    2. Parses cookie expiry timestamps and only trusts the file when
       at least one auth-shaped cookie is still valid (with 5-min
       grace). A stale file is treated the same as a missing one so
       you don't run 30 minutes of tests against a dead session.
    3. When the session is missing or stale **and** autoauth is
       enabled, opens a Chromium window via the autocoder auth
       runner, drives the detected login shape, and captures both
       ``storage_state`` and the ``sessionStorage`` companion. The
       popup blocks until you complete MFA.

    Whenever the files already exist AND the cookies are fresh, this
    is a no-op.
    """
    if _session_live():
        return
    if not settings.AUTOCODER_AUTOAUTH:
        return
    if settings.storage_state_ready():
        # File is on disk but expired — delete it so auth-first
        # doesn't short-circuit via the "storage present" fast path
        # inside ``_materialise_auth``.
        try:
            settings.STORAGE_STATE.unlink()
        except OSError:
            pass

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

    if not settings.LOGIN_URL:
        pytest.skip(
            "autoauth: no .auth/user.json and LOGIN_URL is not set in .env"
        )
        return

    autocoder_settings = load_settings()
    with open_shared_session(autocoder_settings, use_storage_state=False) as shared:
        try:
            goto_resilient(
                shared.page,
                settings.LOGIN_URL,
                nav_timeout_ms=autocoder_settings.browser.extraction_nav_timeout_ms,
                diagnostics_dir=autocoder_settings.paths.logs_dir,
            )
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"autoauth: could not reach LOGIN_URL: {exc!s}")
            return
        spec = build_auth_spec(
            shared.page,
            login_url=settings.LOGIN_URL,
            storage_state_path=str(settings.STORAGE_STATE),
            success_url_marker=settings.BASE_URL or None,
        )
        if spec is None:
            pytest.skip(
                "autoauth: could not detect a login form or SSO button at "
                f"{settings.LOGIN_URL}"
            )
            return
        spec = spec.model_copy(update={"status": Status.STEPS_READY})
        result = run_auth(spec, autocoder_settings, shared=shared)
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
    return settings.BASE_URL


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):  # noqa: F811
    """Bridge ``HEADLESS`` and ``PW_SLOWMO_MS`` from .env into pytest-playwright.

    By default ``pytest-playwright`` only honours its own ``--headed``
    flag — it does **not** read the ``HEADLESS`` env var. That's why
    ``pytest tests/steps`` is silent even when ``.env`` has
    ``HEADLESS=false``. This fixture closes that gap.

    An explicit ``--headed`` / ``--headless`` CLI flag still wins,
    because those flags mutate ``browser_type_launch_args`` before
    this fixture runs.
    """
    args = dict(browser_type_launch_args)
    if "headless" not in browser_type_launch_args:
        args["headless"] = settings.HEADLESS
    if settings.PW_SLOWMO_MS > 0 and "slow_mo" not in browser_type_launch_args:
        args["slow_mo"] = settings.PW_SLOWMO_MS
    return args


@pytest.fixture
def browser_context_args(browser_context_args, base_url):  # noqa: F811
    args = dict(browser_context_args)
    if base_url:
        args["base_url"] = base_url
    if settings.storage_state_ready():
        args["storage_state"] = str(settings.STORAGE_STATE)
    return args


# Pytest hook — stash each phase's outcome on the item so the context
# fixture can check ``item.rep_call.failed`` on teardown and decide
# whether to keep the trace zip. Without this, the fixture has no way
# to know if the test it just served actually passed.
@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):  # noqa: ARG001
    outcome = yield
    rep = outcome.get_result()
    setattr(item, f"rep_{rep.when}", rep)


def _test_failed(request: pytest.FixtureRequest) -> bool:
    """True when the test in ``request`` failed at any of setup/call/teardown."""
    item = request.node
    for phase in ("setup", "call", "teardown"):
        rep = getattr(item, f"rep_{phase}", None)
        if rep is not None and rep.failed:
            return True
    return False


@pytest.fixture
def context(
    browser: Browser,
    browser_context_args,
    request: pytest.FixtureRequest,
) -> Iterator[BrowserContext]:
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
    instead of the pre-auth consent shell.

    When ``AUTOCODER_TRACE=true`` (default on), the context records
    a Playwright trace for the duration of the test. The zip file is
    kept **only when the test fails** so disk usage stays bounded on
    green runs. View with:

        npx playwright show-trace manifest/traces/<file>.zip
    """
    ctx = browser.new_context(**browser_context_args)
    _inject_session_storage(ctx)

    trace_path: Path | None = None
    if settings.AUTOCODER_TRACE:
        try:
            ctx.tracing.start(screenshots=True, snapshots=True, sources=False)
        except Exception:
            # Some Playwright builds restrict tracing under tight
            # permissions — don't fail the test just because we can't
            # record a trace.
            pass

    try:
        yield ctx
    finally:
        if settings.AUTOCODER_TRACE:
            try:
                if _test_failed(request):
                    traces_dir = settings.MANIFEST_DIR / "traces"
                    traces_dir.mkdir(parents=True, exist_ok=True)
                    # Slug + test name, sanitised for filesystem.
                    node_id = request.node.nodeid.replace("::", "__").replace("/", "_").replace("\\", "_")
                    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in node_id)[:120]
                    trace_path = traces_dir / f"{int(time.time())}_{safe}.zip"
                    ctx.tracing.stop(path=str(trace_path))
                else:
                    # Green test — stop without writing. Playwright
                    # still needs ``stop`` to release buffers.
                    ctx.tracing.stop()
            except Exception:
                # Never let a trace error break test teardown.
                pass
        ctx.close()
        if trace_path is not None:
            # Print the path at end-of-test so the user can click it
            # from the terminal without scrolling through pytest output.
            try:
                reporter = request.config.pluginmanager.get_plugin("terminalreporter")
                if reporter is not None:
                    reporter.write_line(
                        f"[autocoder-trace] {request.node.nodeid} → {trace_path}",
                        yellow=True,
                    )
            except Exception:
                pass


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


# ---------------------------------------------------------------------------
# Auto-report: every `pytest tests/...` invocation writes JUnit XML per-slug
# and (re)generates manifest/report.html with the latest pass/fail state.
# ---------------------------------------------------------------------------


def pytest_configure(config: "pytest.Config") -> None:
    """Auto-add ``--junit-xml=manifest/runs/_pytest_session.xml`` when unset.

    Produces the raw XML that ``pytest_sessionfinish`` below splits per
    slug and hands to ``autocoder.report`` for the HTML dashboard.
    """
    if not settings.AUTOCODER_AUTOREPORT:
        return
    # Respect any explicit --junit-xml / --junitxml from the CLI.
    if config.getoption("--junit-xml", default=None):
        return
    runs_dir = settings.MANIFEST_DIR / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    config.option.xmlpath = str(runs_dir / "_pytest_session.xml")


def _split_junit_per_slug(session_xml: Path, runs_dir: Path) -> list[str]:
    """Copy each testcase into a per-slug JUnit XML so ``build_report``
    can show one row per generated suite. Returns the list of slugs
    touched this session.
    """
    import xml.etree.ElementTree as ET

    if not session_xml.exists():
        return []
    try:
        tree = ET.parse(session_xml)
    except ET.ParseError:
        return []
    root = tree.getroot()

    by_slug: dict[str, list[ET.Element]] = {}
    for case in root.iter("testcase"):
        classname = case.attrib.get("classname", "")
        # e.g. ``tests.steps.test_catalog`` → ``catalog``
        last = classname.split(".")[-1]
        if not last.startswith("test_"):
            continue
        slug = last[len("test_"):]
        by_slug.setdefault(slug, []).append(case)

    touched: list[str] = []
    for slug, cases in by_slug.items():
        out_root = ET.Element("testsuites")
        suite = ET.SubElement(
            out_root,
            "testsuite",
            {
                "name": f"test_{slug}",
                "tests": str(len(cases)),
                "failures": str(sum(1 for c in cases if c.find("failure") is not None)),
                "errors": str(sum(1 for c in cases if c.find("error") is not None)),
            },
        )
        for c in cases:
            suite.append(c)
        out_path = runs_dir / f"{slug}.xml"
        ET.ElementTree(out_root).write(out_path, encoding="utf-8", xml_declaration=True)
        touched.append(slug)
    return touched


def pytest_sessionfinish(session: "pytest.Session", exitstatus: int) -> None:
    """Regenerate manifest/report.html using the JUnit XML this run produced.

    Silent no-op when the autocoder package isn't importable (someone
    running just the generated suite in a bare venv) or when the user
    opted out via ``AUTOCODER_AUTOREPORT=false``.
    """
    if not settings.AUTOCODER_AUTOREPORT:
        return
    runs_dir = settings.MANIFEST_DIR / "runs"
    session_xml = runs_dir / "_pytest_session.xml"

    touched = _split_junit_per_slug(session_xml, runs_dir)
    if not touched:
        return

    try:
        from autocoder.config import load_settings
        from autocoder.report import build_report, render_html_report
    except Exception:
        return

    try:
        autocoder_settings = load_settings()
        data = build_report(autocoder_settings, run_pytest=False)
        html_path = settings.MANIFEST_DIR / "report.html"
        html_path.write_text(render_html_report(data), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(f"[autocoder-report] skipped: {exc!s}", yellow=True)
        return

    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line("")
        reporter.write_line(
            f"[autocoder-report] {len(touched)} slug(s) updated → {html_path}",
            cyan=True,
        )
