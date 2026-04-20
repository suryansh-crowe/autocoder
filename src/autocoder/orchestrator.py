"""End-to-end pipeline that ties every stage together.

Workflow (matches the user-defined flow):

1. **Intake**       — classify URLs, persist nodes to the registry.
2. **Auth-first**   — if any URL needs auth (or any URL is the login
                      itself), build the auth setup before extracting
                      protected pages.
3. **Extraction**   — visit each URL in dependency order using the
                      stored ``storage_state`` when applicable.
4. **POM plan**     — single LLM call per URL, JSON output, validated
                      against the catalog.
5. **POM render**   — deterministic template -> ``tests/pages/<slug>_page.py``.
6. **Feature plan** — single LLM call per URL covering the requested
                      tiers, validated against POM method names.
7. **Feature render** -> ``tests/features/<slug>.feature``.
8. **Steps render** -> ``tests/steps/test_<slug>.py`` (zero LLM tokens).
9. **Persist**      — registry status updates + run log entry.

Reruns reuse cached extractions and plans by fingerprint, so unchanged
pages cost nothing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from autocoder import logger
from autocoder.config import Settings, ensure_dirs
from autocoder.extract.auth_probe import build_auth_spec
from autocoder.extract.auth_runner import run_auth
from autocoder.extract.browser import (
    AuthUnreachable,
    BrowserSession,
    goto_resilient,
    open_session,
    open_shared_session,
)
from contextlib import contextmanager, nullcontext
from autocoder.extract.inspector import extract_page
from autocoder.generate.auth_setup import render_auth_setup
from autocoder.generate.feature import render_feature
from autocoder.generate.pom import render_pom
from autocoder.generate.steps import render_steps
from autocoder.intake.classifier import classify_urls, looks_like_login_url
from autocoder.intake.graph import build_dependency_graph, topological_order
from autocoder.llm.factory import get_llm_client
from autocoder.llm.ollama_client import OllamaClient
from autocoder.llm.plans import generate_feature_plan, generate_pom_plan
from autocoder.registry.diff import diff_extractions
from autocoder.registry.store import RegistryStore, fingerprint_extraction
from autocoder.models import (
    AuthSpec,
    PageExtraction,
    Registry,
    Status,
    URLKind,
    URLNode,
)
from autocoder.utils import page_class_name


DEFAULT_TIERS = ["smoke", "happy", "validation"]


def _strip_step_bodies_for_heal(source: str) -> str:
    """Rewrite every step function body to ``raise NotImplementedError``.

    Used when the freshly-rendered test file fails ``ast.parse`` — a
    single malformed body would otherwise block pytest collection for
    the entire tests directory. Stripping to
    ``NotImplementedError("Implement step: <text>")`` restores a
    parseable module so pytest can collect, and the same shape is what
    the auto-heal scanner looks for — it will refill each body via
    the LLM on the next heal pass.

    Line-based rewrite (not AST, because the AST is what's broken).
    A step function is identified by: ``def _<name>(<fixture>: <Cls>) -> None:``
    immediately preceded by one or more ``@given/@when/@then`` decorators.
    Everything between that header and the next blank-line-then-decorator
    boundary is replaced with a single ``raise NotImplementedError(...)``.
    """
    import re as _re

    lines = source.splitlines(keepends=False)
    out: list[str] = []
    i = 0
    header_re = _re.compile(
        r"^\s*def\s+(_[A-Za-z0-9_]+)\s*\([a-zA-Z0-9_]+\s*:\s*[A-Za-z0-9_]+\s*\)\s*->\s*None\s*:\s*$"
    )
    parse_re = _re.compile(r"parsers\.parse\(\s*['\"](.+?)['\"]\s*\)", _re.DOTALL)
    while i < len(lines):
        line = lines[i]
        m = header_re.match(line)
        if not m:
            out.append(line)
            i += 1
            continue
        # Walk backward through contiguous decorator lines to find
        # the @<keyword>(parsers.parse('<text>')) that owns this function.
        step_text = m.group(1).lstrip("_").replace("_", " ")
        j = i - 1
        while j >= 0 and lines[j].lstrip().startswith("@"):
            pm = parse_re.search(lines[j])
            if pm:
                step_text = pm.group(1)
                break
            j -= 1
        out.append(line)
        # Skip the original body — everything until a blank line
        # followed by a decorator / EOF.
        i += 1
        while i < len(lines):
            nxt = lines[i]
            stripped = nxt.strip()
            if stripped == "" and i + 1 < len(lines) and lines[i + 1].lstrip().startswith("@"):
                break
            if stripped.startswith("@") and not nxt.startswith(" "):
                break
            i += 1
        # Emit a clean stub body at the function's indentation level.
        escaped = step_text.replace("\\", "\\\\").replace('"', '\\"')
        out.append(f'    raise NotImplementedError("Implement step: {escaped}")')
        out.append("")
    return "\n".join(out) + ("\n" if source.endswith("\n") else "")


@dataclass
class GenerateOptions:
    urls: list[str]
    tiers: list[str]
    force: bool = False
    skip_llm: bool = False


@dataclass
class StageResult:
    node: URLNode
    extraction: PageExtraction | None
    pom_path: Path | None
    feature_path: Path | None
    steps_path: Path | None
    issues: list[str]


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def run_generate(settings: Settings, opts: GenerateOptions) -> list[StageResult]:
    """Main entry point used by ``autocoder generate``."""
    ensure_dirs(settings)
    logger.init(settings.paths.logs_dir, level=settings.log_level)
    run_started = time.monotonic()
    logger.stage(
        "run_start",
        urls=len(opts.urls),
        tiers=",".join(opts.tiers),
        force=opts.force,
        skip_llm=opts.skip_llm,
        log_level=settings.log_level,
        manifest=str(settings.paths.manifest_dir),
    )

    store = RegistryStore(settings.paths.registry_path)
    registry = store.load()
    if not registry.base_url:
        registry.base_url = settings.base_url
        logger.info("registry_base_url_seeded", base_url=settings.base_url)

    # Stage 1: intake
    logger.stage("intake", urls=len(opts.urls))
    nodes, detected_login = classify_urls(opts.urls, settings)
    for node in nodes:
        store.upsert_node(registry, node)
    logger.ok(
        "intake_done",
        nodes=len(nodes),
        detected_login=logger.safe_url(detected_login) if detected_login else "",
    )

    # Stage 1b: homepage probe — if ``base_url`` is set and it was not
    # already one of the input URLs, check whether fetching it
    # anonymously redirects to a login-shaped URL. A positive here is
    # enough to mark the whole run auth-required even if every input
    # URL happened to render a neutral shell.
    homepage_detected_login = _probe_homepage(registry, settings, opts.urls)
    if homepage_detected_login:
        detected_login = detected_login or homepage_detected_login

    # Stage 2: auth-first — establish AuthSpec if anything needs it
    needs_auth = any(n.requires_auth for n in registry.nodes.values())
    has_login_signal = (
        bool(settings.login_url)
        or bool(detected_login)
        or any(
            n.kind in (URLKind.LOGIN, URLKind.REDIRECT_TO_LOGIN)
            or (n.kind == URLKind.UNKNOWN and looks_like_login_url(n.url))
            for n in registry.nodes.values()
        )
    )
    logger.stage(
        "auth_first",
        needs_auth=needs_auth,
        has_login_signal=has_login_signal,
    )
    if registry.auth is None:
        registry.auth = _maybe_seed_auth(registry, settings, detected_login)

    # When the run touches authenticated URLs, we open ONE long-lived
    # browser context for the auth stage AND every URL extraction.
    # This preserves MSAL's in-memory state + sessionStorage (which
    # Playwright's storage_state does not persist), so SPAs that
    # require the user's SSO account to be "hydrated" in the tab show
    # the authenticated DOM on every subsequent URL instead of the
    # consent shell.
    shared_ctx = (
        open_shared_session(settings, use_storage_state=True)
        if registry.auth is not None or needs_auth
        else nullcontext(None)
    )
    if registry.auth is None:
        logger.info(
            "auth_skipped",
            reason="no_login_signal" if not has_login_signal else "no_authenticated_urls",
            needs_auth=needs_auth,
            has_login_signal=has_login_signal,
        )

    results: list[StageResult] = []
    client = None
    with shared_ctx as shared:
        # Auth-first inside the shared session so the context that
        # completes the sign-in is the same one every URL extraction
        # uses afterwards.
        if registry.auth is not None:
            _materialise_auth(registry, settings, store, shared=shared)

        # Stage 3+: per-URL pipeline in dependency order
        deps = build_dependency_graph(
            list(registry.nodes.values()),
            registry.auth.login_url if registry.auth else None,
        )
        ordered = topological_order(list(registry.nodes.values()), deps)
        logger.info(
            "pipeline_order",
            count=len(ordered),
            order=",".join(n.slug for n in ordered),
        )

        if not opts.skip_llm:
            client = get_llm_client(settings)
            backend = "azure_openai" if settings.use_azure_openai else "ollama"
            endpoint = (
                settings.azure_openai.endpoint
                if settings.use_azure_openai
                else settings.ollama.endpoint
            )
            logger.info("llm_check", backend=backend, endpoint=endpoint)
            if not client.is_available():
                logger.die(
                    "llm_unreachable",
                    backend=backend,
                    endpoint=endpoint,
                    hint=(
                        "Verify the Azure endpoint/deployment/api-key." if settings.use_azure_openai
                        else "Start the container; see readme/09_llm.md."
                    ),
                )
            logger.ok("llm_ready", backend=backend, endpoint=endpoint)
        else:
            logger.warn("llm_skipped_by_flag", flag="--skip-llm")

        try:
            for idx, node in enumerate(ordered, start=1):
                logger.stage(
                    "url_begin",
                    position=f"{idx}/{len(ordered)}",
                    slug=node.slug,
                    url=logger.safe_url(node.url),
                    kind=node.kind.value,
                    status=node.status.value,
                )

                if node.kind == URLKind.LOGIN and registry.auth and node.url == registry.auth.login_url:
                    node.status = Status.COMPLETE
                    store.upsert_node(registry, node)
                    logger.ok(
                        "url_skipped",
                        slug=node.slug,
                        reason="login_url_covered_by_auth_setup",
                    )
                    continue

                # Liveness check on the shared session. Long LLM phases
                # (POM + feature plans + autoheal) sit idle for minutes
                # on phi4:14b CPU — enough time for the headed Chromium
                # window to die (user closes it, Windows reaps it, OOM,
                # etc.). Without this check, every URL after the first
                # one fails instantly with "Target page, context or
                # browser has been closed". When the session is dead we
                # reopen a fresh one and let silent_reauth rehydrate
                # MSAL from the still-valid storage_state on disk.
                shared = _ensure_shared_alive(shared, settings, registry)

                try:
                    result = _process_url(
                        node, registry, settings, store, client, opts, shared=shared
                    )
                except Exception as exc:  # noqa: BLE001
                    # One URL must not be able to abort the whole run.
                    # Mark it failed, log, persist, and continue with the
                    # next URL. The user can rerun once the cause is fixed.
                    node.status = Status.FAILED
                    store.upsert_node(registry, node)
                    store.save(registry)
                    logger.error(
                        "url_failed",
                        slug=node.slug,
                        err=str(exc),
                        err_type=type(exc).__name__,
                    )
                    results.append(StageResult(
                        node=node,
                        extraction=None,
                        pom_path=Path(node.pom_path) if node.pom_path else None,
                        feature_path=Path(node.feature_path) if node.feature_path else None,
                        steps_path=Path(node.steps_path) if node.steps_path else None,
                        issues=[f"{type(exc).__name__}: {exc!s}"],
                    ))
                    continue
                results.append(result)
                store.upsert_node(registry, result.node)
                store.save(registry)
                logger.ok(
                    "url_done",
                    slug=node.slug,
                    status=result.node.status.value,
                    pom=bool(result.pom_path),
                    feature=bool(result.feature_path),
                    steps=bool(result.steps_path),
                    issues=len(result.issues),
                )
        finally:
            if client is not None:
                client.close()

    store.save(registry)
    complete = sum(1 for r in results if r.node.status == Status.COMPLETE)
    needs_impl = sum(1 for r in results if r.node.status == Status.NEEDS_IMPLEMENTATION)
    failed = sum(1 for r in results if r.node.status == Status.FAILED)
    duration = f"{time.monotonic() - run_started:.2f}s"
    if needs_impl or failed:
        logger.warn(
            "run_done_with_issues",
            processed=len(results),
            complete=complete,
            needs_implementation=needs_impl,
            failed=failed,
            duration=duration,
            hint=(
                "Generation finished, but some URLs produced placeholder steps or "
                "failed outright. See steps_incomplete / url_failed entries above."
            ),
        )
    else:
        logger.ok(
            "run_done",
            processed=len(results),
            complete=complete,
            duration=duration,
        )
    return results


def run_status(settings: Settings) -> Registry:
    store = RegistryStore(settings.paths.registry_path)
    return store.load()


# ---------------------------------------------------------------------------
# Integrated generate → test → heal loop
# ---------------------------------------------------------------------------


@dataclass
class PytestOutcome:
    slug: str
    passed: bool
    failure_count: int
    junit_path: Path


@dataclass
class CycleOutcome:
    """What ``run_full_cycle`` produces.

    ``generation`` is the :func:`run_generate` result; everything else
    is verification telemetry collected by the loop.
    """

    generation: list[StageResult]
    verification: dict[str, PytestOutcome]
    heal_attempts: dict[str, int]
    final_status: dict[str, Status]


def _run_pytest_for_slug(slug: str, settings: Settings) -> PytestOutcome:
    """Run pytest for a single slug's test module and return pass/fail."""
    from autocoder.heal.pytest_failures import run_pytest_capture

    test_file = settings.paths.steps_dir / f"test_{slug}.py"
    junit_dir = settings.paths.manifest_dir / "runs"
    junit_dir.mkdir(parents=True, exist_ok=True)
    junit_path = junit_dir / f"{slug}.xml"
    if not test_file.exists():
        logger.warn("pytest_skipped_missing", slug=slug, path=str(test_file))
        return PytestOutcome(slug=slug, passed=False, failure_count=0, junit_path=junit_path)
    failures = run_pytest_capture(test_paths=[test_file], junit_path=junit_path)
    passed = not failures
    logger.info(
        "pytest_outcome",
        slug=slug,
        passed=passed,
        failures=len(failures),
        junit=str(junit_path),
    )
    return PytestOutcome(
        slug=slug,
        passed=passed,
        failure_count=len(failures),
        junit_path=junit_path,
    )


def run_full_cycle(
    settings: Settings,
    opts: GenerateOptions,
    *,
    max_heal_attempts: int = 3,
) -> CycleOutcome:
    """End-to-end lifecycle: generate → pytest → heal → pytest, on repeat.

    Parameters
    ----------
    max_heal_attempts:
        Upper bound on heal passes per failing slug. Defaults to 3 — a
        sensible number for a local LLM (`phi4:14b` on CPU): high
        enough to recover from the common runtime failure modes
        (`locator_not_found`, `wrong_kind`, `disabled`, `timeout`)
        without blowing the wall-clock budget. Set `0` to run pytest
        once and skip healing entirely.

    Flow
    ----
    1. :func:`run_generate` produces POM / feature / steps files.
    2. For every slug whose generation ended in ``COMPLETE`` or
       ``NEEDS_IMPLEMENTATION`` and has a ``tests/steps/test_<slug>.py``
       file, pytest is invoked against that single file and the JUnit
       XML is stored under ``manifest/runs/<slug>.xml``.
    3. While any slug is failing and ``heal_attempts[slug] <
       max_heal_attempts``, ``heal_steps(..., from_pytest=True)`` is
       called for each failing slug; the runtime errors drive
       LLM-generated patches (validated and written into the step
       files). Pytest re-runs for just those slugs.
    4. The loop stops when every slug passes OR no heal attempt
       produced any change (no point burning the remaining budget).
    5. The registry is updated: passing slugs go to ``VERIFIED``,
       failing ones keep ``NEEDS_IMPLEMENTATION`` (or their prior
       state) and gain ``heal_attempts`` + ``last_pytest_outcome``
       telemetry.
    """
    from autocoder.heal import HealOptions, heal_steps

    gen_results = run_generate(settings, opts)
    store = RegistryStore(settings.paths.registry_path)
    registry = store.load()

    # Build the verification set: slugs with a steps file, reached at
    # least ``COMPLETE``, and have a regenerated test on disk.
    verifiable: list[str] = []
    for r in gen_results:
        if r.node.status in (Status.COMPLETE, Status.NEEDS_IMPLEMENTATION) and r.steps_path:
            verifiable.append(r.node.slug)

    logger.stage(
        "run_verification",
        slugs=",".join(verifiable) or "(none)",
        count=len(verifiable),
        max_heal_attempts=max_heal_attempts,
    )

    verification: dict[str, PytestOutcome] = {}
    heal_attempts: dict[str, int] = {slug: 0 for slug in verifiable}

    # First pass — run pytest for each verifiable slug.
    for slug in verifiable:
        verification[slug] = _run_pytest_for_slug(slug, settings)

    # Heal loop.
    if max_heal_attempts > 0:
        for attempt in range(1, max_heal_attempts + 1):
            failing = [s for s in verifiable if not verification[s].passed]
            if not failing:
                break
            logger.stage(
                "run_heal_attempt",
                attempt=attempt,
                max=max_heal_attempts,
                failing=",".join(failing),
            )
            any_applied = 0
            for slug in failing:
                heal_results = heal_steps(
                    settings,
                    HealOptions(slug=slug, from_pytest=True),
                )
                applied = sum(1 for h in heal_results if h.applied)
                heal_attempts[slug] = attempt
                any_applied += applied
                logger.info(
                    "run_heal_slug_done",
                    slug=slug,
                    attempt=attempt,
                    total=len(heal_results),
                    applied=applied,
                )
            if any_applied == 0:
                logger.warn(
                    "run_heal_no_progress",
                    attempt=attempt,
                    hint=(
                        "no step bodies changed this attempt — the LLM's "
                        "suggestions were either cached rejections or "
                        "validator rejections. Stopping the loop early to "
                        "save cycles."
                    ),
                )
                break
            # Re-run pytest for just the failing slugs.
            for slug in failing:
                verification[slug] = _run_pytest_for_slug(slug, settings)

    # Persist verification outcome to the registry.
    final_status: dict[str, Status] = {}
    now = _now_iso()
    for slug, outcome in verification.items():
        node = next((n for n in registry.nodes.values() if n.slug == slug), None)
        if node is None:
            continue
        node.last_verified_at = now
        node.last_pytest_outcome = "pass" if outcome.passed else "fail"
        node.heal_attempts = heal_attempts.get(slug, 0)
        if outcome.passed:
            node.status = Status.VERIFIED
        elif node.status == Status.COMPLETE:
            # Generation finished but tests still fail — promote to
            # needs_implementation so the summary surfaces it.
            node.status = Status.NEEDS_IMPLEMENTATION
        final_status[slug] = node.status
        store.upsert_node(registry, node)
    store.save(registry)

    verified = sum(1 for o in verification.values() if o.passed)
    still_failing = sum(1 for o in verification.values() if not o.passed)
    logger.ok(
        "run_full_cycle_done",
        verifiable=len(verifiable),
        verified=verified,
        still_failing=still_failing,
        total_heal_attempts=sum(heal_attempts.values()),
    )
    return CycleOutcome(
        generation=gen_results,
        verification=verification,
        heal_attempts=heal_attempts,
        final_status=final_status,
    )


def _now_iso() -> str:
    import datetime as _dt

    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Auth-first
# ---------------------------------------------------------------------------


def _probe_homepage(
    registry: Registry,
    settings: Settings,
    input_urls: list[str],
) -> str | None:
    """Quickly check whether the base URL is gated by authentication.

    Returns a discovered login URL if the homepage redirects to one
    (or renders a login form), ``None`` otherwise. The purpose is to
    catch sites whose input-URL list did not include the base URL and
    where every classified URL happens to render a neutral anonymous
    shell — we still want auth-first to fire.
    """
    base = (settings.base_url or "").rstrip("/")
    if not base:
        return None
    # Skip when the base URL (or something that normalises to it) is
    # already an input — the regular classifier handled it.
    existing = {u.rstrip("/") for u in input_urls if u}
    if base in existing:
        return None

    logger.info("stage:homepage_probe", base_url=logger.safe_url(base))
    try:
        # Reuse the classifier's single-URL path by running it through
        # the same function; minimal overhead and consistent logging.
        probe_nodes, probe_login = classify_urls([base], settings)
    except Exception as exc:  # noqa: BLE001
        logger.warn("homepage_probe_failed", err=str(exc))
        return None

    if not probe_nodes:
        return None
    probe = probe_nodes[0]

    # Persist a homepage node only when it contributes something the
    # rest of the pipeline needs. We do not want to silently add URLs
    # the user never asked for.
    if probe.kind in (URLKind.LOGIN, URLKind.REDIRECT_TO_LOGIN):
        # Base URL itself is login-shaped — seed the login URL and
        # return without adding the homepage node to the registry.
        seed = probe.redirects_to or probe.url
        logger.info(
            "homepage_probe_auth_detected",
            base_url=logger.safe_url(base),
            kind=probe.kind.value,
            detected_login=logger.safe_url(seed),
        )
        return seed

    # Base URL rendered without redirect. Drop the probe node to avoid
    # polluting the registry but keep the signal for the caller.
    logger.info(
        "homepage_probe_clear",
        base_url=logger.safe_url(base),
        kind=probe.kind.value,
        requires_auth=probe.requires_auth,
    )
    return None


def _maybe_seed_auth(
    registry: Registry,
    settings: Settings,
    detected_login: str | None,
) -> AuthSpec | None:
    """Seed an :class:`AuthSpec` if *any* signal points at a login URL.

    Signals considered (first match wins for the login URL itself):

    1. ``LOGIN_URL`` in settings — the user's explicit declaration.
    2. A login URL discovered by the classifier via password-field probe.
    3. A node with ``kind=LOGIN`` or ``kind=REDIRECT_TO_LOGIN``.
    4. A node that pattern-matches ``looks_like_login_url`` even if the
       classifier timed out before it could confirm the form.

    We also gate on ``needs_auth``: any of the above signals, *or* any
    node already flagged ``requires_auth=True`` (for example because a
    protected URL was unreachable anonymously while a login URL is
    configured), is enough to justify running auth-first.
    """
    candidates: list[tuple[str, str]] = []
    if settings.login_url:
        candidates.append((settings.login_url, "env:LOGIN_URL"))
    if detected_login:
        candidates.append((detected_login, "classifier_detection"))
    for n in registry.nodes.values():
        if n.kind in (URLKind.LOGIN, URLKind.REDIRECT_TO_LOGIN):
            candidates.append(
                (n.redirects_to or n.url, f"node:{n.slug}:{n.kind.value}")
            )
    for n in registry.nodes.values():
        if n.kind == URLKind.UNKNOWN and looks_like_login_url(n.url):
            candidates.append((n.url, f"node:{n.slug}:path_hint"))

    needs_auth = (
        any(n.requires_auth for n in registry.nodes.values())
        or any(
            n.kind in (URLKind.LOGIN, URLKind.REDIRECT_TO_LOGIN, URLKind.AUTHENTICATED)
            for n in registry.nodes.values()
        )
        or bool(settings.login_url)
    )

    if not needs_auth:
        return None

    if not candidates:
        logger.warn(
            "auth_needed_but_no_login_url",
            hint="Set LOGIN_URL in .env or include the login URL in the input list.",
        )
        return None

    # De-dupe while preserving priority order.
    seen: set[str] = set()
    ordered: list[tuple[str, str]] = []
    for url, source in candidates:
        if url and url not in seen:
            seen.add(url)
            ordered.append((url, source))

    login_url, source = ordered[0]
    logger.info(
        "auth_seeded",
        login_url=logger.safe_url(login_url),
        source=source,
        storage_state=str(settings.paths.storage_state),
        candidates=len(ordered),
    )
    return AuthSpec(login_url=login_url, storage_state_path=str(settings.paths.storage_state))


def _materialise_auth(
    registry: Registry,
    settings: Settings,
    store: RegistryStore,
    *,
    shared: BrowserSession | None = None,
) -> None:
    """Probe the login page, render the auth setup test, persist the spec.

    When ``shared`` is provided, the auth probe and the live login
    runner drive **that** page instead of opening their own throwaway
    browser. The in-memory MSAL state and ``sessionStorage`` that the
    login populates therefore survive into every URL extraction that
    follows in the same run.
    """
    auth = registry.auth
    if auth is None:
        return
    setup_path = settings.paths.auth_setup_dir / "test_auth_setup.py"
    storage_ok = _storage_state_usable(settings)

    # Fast path: template rendered AND a live session file exists.
    # Both halves are required — ``status == STEPS_READY`` only means
    # "we wrote the setup file on the last run", it does not imply the
    # runner actually captured a session. Without ``.auth/user.json``
    # we must retry the runner.
    if auth.status == Status.STEPS_READY and setup_path.exists() and storage_ok:
        logger.info(
            "auth_setup_reused",
            path=str(setup_path),
            storage_state=str(settings.paths.storage_state),
            reason="template_and_session_present",
        )
        return

    # Warm path: template is already on disk and the spec is populated
    # with real selectors — only the session was never captured.
    # Skip the re-probe + re-render and jump straight to the runner.
    spec_has_selectors = (
        auth.sso_button_selector is not None
        or auth.username_selector is not None
        or auth.password_selector is not None
    )
    if (
        auth.status == Status.STEPS_READY
        and setup_path.exists()
        and spec_has_selectors
        and not storage_ok
    ):
        logger.info(
            "auth_session_retry",
            path=str(setup_path),
            reason="template_ready_but_no_storage",
            hint=(
                "previous run rendered the auth-setup but did not capture "
                "a session; re-running the in-process auth runner now"
            ),
        )
        _run_and_persist_auth(auth, registry, settings, store, shared=shared)
        return

    # Trust path: a valid ``storage_state`` file exists but the
    # registry does not yet record ``STEPS_READY`` (common after
    # ``manifest/registry.yaml`` was deleted / a fresh checkout uses
    # a pre-captured session). Probing ``/login`` here is fragile:
    # many SSO-backed SPAs 404 or redirect the login route for
    # already-authenticated users, which would make
    # ``build_auth_spec`` return ``None`` and the whole URL loop
    # bail with ``url_skipped_awaiting_auth``. Instead, trust the
    # storage file. If it turns out to be stale the first URL's
    # extraction will hit the consent shell and the escalation +
    # silent-reauth paths recover on the spot.
    if storage_ok and auth.status != Status.STEPS_READY:
        logger.info(
            "auth_storage_trusted",
            storage_state=str(settings.paths.storage_state),
            hint=(
                "valid storage_state on disk; skipping auth probe and "
                "trusting the session. If the session is actually "
                "stale, extraction will detect the consent shell and "
                "trigger silent re-auth."
            ),
        )
        registry.auth = auth.model_copy(update={"status": Status.STEPS_READY})
        store.save(registry)
        return

    logger.info("auth_probe_start", url=logger.safe_url(auth.login_url))
    with _session_or_open(shared, settings, use_storage=False) as sess:
        try:
            diag = goto_resilient(
                sess.page,
                auth.login_url,
                nav_timeout_ms=settings.browser.extraction_nav_timeout_ms,
                diagnostics_dir=settings.paths.logs_dir,
            )
            logger.info(
                "auth_probe_navigated",
                url=logger.safe_url(auth.login_url),
                **diag.to_dict(),
            )
        except AuthUnreachable as exc:
            logger.warn(
                "auth_probe_failed",
                url=logger.safe_url(auth.login_url),
                err=str(exc),
                **exc.diag.to_dict(),
                hint=(
                    "Login page did not commit in time — likely an SSO redirect "
                    "or slow SPA shell. Check the screenshot/HTML in the logs dir."
                ),
            )
            return
        except Exception as exc:  # noqa: BLE001
            logger.warn(
                "auth_probe_failed",
                url=logger.safe_url(auth.login_url),
                err=str(exc),
            )
            return
        seeded = build_auth_spec(
            sess.page,
            login_url=auth.login_url,
            storage_state_path=str(settings.paths.storage_state),
            success_url_marker=settings.base_url or None,
        )
    if seeded is None:
        logger.warn(
            "auth_form_not_detected",
            url=logger.safe_url(auth.login_url),
            hint=(
                "Login page did not expose a password input OR a recognised "
                "SSO provider button. Set AUTH_MSFT_* overrides if the tenant "
                "uses custom markup."
            ),
        )
        return
    logger.ok(
        "auth_mode_detected",
        url=logger.safe_url(auth.login_url),
        auth_kind=seeded.auth_kind,
        requires_external_completion=seeded.requires_external_completion,
        username_strategy=(
            seeded.username_selector.strategy.value if seeded.username_selector else None
        ),
        password_strategy=(
            seeded.password_selector.strategy.value if seeded.password_selector else None
        ),
        submit_strategy=(
            seeded.submit_selector.strategy.value if seeded.submit_selector else None
        ),
        continue_strategy=(
            seeded.continue_selector.strategy.value if seeded.continue_selector else None
        ),
        sso_strategy=(
            seeded.sso_button_selector.strategy.value if seeded.sso_button_selector else None
        ),
        username_env_present=settings.secret_present("LOGIN_USERNAME"),
        password_env_present=settings.secret_present("LOGIN_PASSWORD"),
        notes=",".join(seeded.notes or []) or None,
    )

    merged = auth.model_copy(
        update={
            "auth_kind": seeded.auth_kind,
            "username_selector": seeded.username_selector,
            "password_selector": seeded.password_selector,
            "submit_selector": seeded.submit_selector,
            "continue_selector": seeded.continue_selector,
            "sso_button_selector": seeded.sso_button_selector,
            "requires_external_completion": seeded.requires_external_completion,
            "success_indicator_url_contains": seeded.success_indicator_url_contains,
            "notes": list(seeded.notes or []),
        }
    )

    settings.paths.auth_setup_dir.mkdir(parents=True, exist_ok=True)
    existed = setup_path.exists()
    setup_path.write_text(
        render_auth_setup(merged, storage_state_path=str(settings.paths.storage_state)),
        encoding="utf-8",
    )
    merged = merged.model_copy(update={"setup_path": str(setup_path), "status": Status.STEPS_READY})
    registry.auth = merged
    store.save(registry)
    logger.ok(
        "auth_setup_written",
        path=str(setup_path),
        action="updated" if existed else "created",
    )

    # Actually perform the login so the rest of the run has a session.
    # If credentials are missing we log and return — the rendered
    # auth-setup test is still on disk for the user to run manually.
    _run_and_persist_auth(merged, registry, settings, store, shared=shared)


def _run_and_persist_auth(
    spec: AuthSpec,
    registry: Registry,
    settings: Settings,
    store: RegistryStore,
    *,
    shared: BrowserSession | None = None,
) -> None:
    """Execute the in-process auth runner and persist the outcome.

    Three terminal states:

    * **ok** — storage_state captured, non-LOGIN nodes stale-marked for
      re-extraction under the new session.
    * **awaiting_external_completion** — the runner made progress but
      the flow needs a human (magic link, OTP, MFA the runner could
      not satisfy automatically). `AuthSpec.status` is downgraded to
      ``NEEDS_IMPLEMENTATION`` so the run summary surfaces it.
    * **any other reason** — logged as ``auth_session_not_captured``
      with mode-aware hints. Registry auth status is *rolled back* to
      ``PENDING`` so the next run re-tries the runner instead of
      silently reusing a "setup ready" marker that never had a session.
    """
    result = run_auth(spec, settings, shared=shared)
    if result.ok:
        logger.ok(
            "auth_session_captured",
            storage_state=str(settings.paths.storage_state),
            final_url=logger.safe_url(result.final_url),
            auth_kind=spec.auth_kind,
            elapsed=f"{result.elapsed_s:.2f}s",
        )
        # "Settle" step: _wait_success may have returned while the
        # browser is still on the SSO return URL (often ``/login`` for
        # apps whose MSAL redirect_uri is a non-existent SPA route).
        # If extraction starts from there, the SPA's first goto to a
        # protected URL can bounce right back to /login because MSAL
        # hasn't hydrated yet — and extraction captures the 404 DOM.
        # Proactively navigate the shared page to ``base_url`` now,
        # which gives MSAL one clean boot on a real route. Every
        # per-URL extraction afterwards starts from a hydrated,
        # authenticated SPA instead of the raw OAuth-return landing.
        _settle_after_auth(shared, settings, result.final_url)
        stale = 0
        for n in registry.nodes.values():
            if n.kind == URLKind.LOGIN:
                continue
            if n.status in (Status.COMPLETE, Status.NEEDS_IMPLEMENTATION, Status.EXTRACTED):
                n.status = Status.PENDING
                n.last_fingerprint = None
                n.notes.append("stale_after_auth_capture")
                stale += 1
        if stale:
            store.save(registry)
            logger.info("auth_post_capture_invalidated", count=stale)
        return

    if result.reason == "awaiting_external_completion":
        registry.auth = spec.model_copy(
            update={
                "requires_external_completion": True,
                "status": Status.NEEDS_IMPLEMENTATION,
                "notes": list(spec.notes or []) + [
                    f"runner_paused_after={result.diagnostics.get('second_step', '?')}",
                ],
            }
        )
        store.save(registry)
        logger.warn(
            "auth_session_awaiting_external",
            reason=result.reason,
            auth_kind=spec.auth_kind,
            final_url=logger.safe_url(result.final_url),
            elapsed=f"{result.elapsed_s:.2f}s",
            hint=(result.diagnostics or {}).get(
                "hint", "complete the external step and rerun"
            ),
        )
        return

    # Any other failure: roll back ``status`` so the next run retries
    # the runner instead of short-circuiting through ``auth_setup_reused``.
    # Keep the selectors that the probe captured — they are still
    # correct, there is just no live session tied to them.
    registry.auth = spec.model_copy(update={"status": Status.PENDING})
    store.save(registry)

    is_sso = spec.auth_kind in ("sso_microsoft", "sso_generic")
    if spec.auth_kind == "form":
        hint = (
            "Set LOGIN_USERNAME/LOGIN_PASSWORD in .env and rerun, "
            "or run `pytest tests/auth_setup -m auth_setup` with "
            "HEADLESS=false to complete the flow manually."
        )
    elif is_sso:
        hint = (
            "SSO flow did not finish. Rerun with HEADLESS=false and "
            "complete MFA in the visible browser. If the Sign-in "
            "button stayed disabled, see auth_sso_button_* events for "
            "the consent-checkbox unblock attempt."
        )
    else:
        hint = (
            "Auth runner did not capture a session. See diag_* fields "
            "below; rerun headed to complete any interactive step."
        )
    logger.warn(
        "auth_session_not_captured",
        reason=result.reason,
        auth_kind=spec.auth_kind,
        final_url=logger.safe_url(result.final_url),
        elapsed=f"{result.elapsed_s:.2f}s",
        hint=hint,
        **{f"diag_{k}": v for k, v in (result.diagnostics or {}).items()},
    )


# ---------------------------------------------------------------------------
# Per-URL pipeline
# ---------------------------------------------------------------------------


def _process_url(
    node: URLNode,
    registry: Registry,
    settings: Settings,
    store: RegistryStore,
    client,  # OllamaClient | AzureOpenAIClient | None — typed loosely on purpose
    opts: GenerateOptions,
    *,
    shared: BrowserSession | None = None,
) -> StageResult:
    issues: list[str] = []
    # Storage is used when:
    #   * the node itself is known to need auth, OR
    #   * auth-first has materialised a usable storage_state — in which
    #     case we reuse it for every non-LOGIN node, INCLUDING those
    #     classified PUBLIC. The anonymous classification was made
    #     against the pre-login shell; a PUBLIC verdict there is not
    #     evidence that the authenticated view is identical. Adding
    #     cookies to a genuinely-public request is free.
    auth_ready = (
        registry.auth is not None
        and registry.auth.status == Status.STEPS_READY
        and settings.paths.storage_state.exists()
        and settings.paths.storage_state.stat().st_size > 0
    )

    # If the node needs auth but no session is available, don't
    # silently generate tests against the anonymous shell — that
    # produces misleading artifacts ("test the real app" pointing at a
    # sign-in prompt). Mark and skip; the user can rerun once
    # `.auth/user.json` has been captured.
    if node.requires_auth and not auth_ready and node.kind != URLKind.LOGIN:
        node.status = Status.NEEDS_IMPLEMENTATION
        note = "skipped_generation_awaiting_auth_session"
        if note not in node.notes:
            node.notes.append(note)
        store.upsert_node(registry, node)
        logger.warn(
            "url_skipped_awaiting_auth",
            slug=node.slug,
            url=logger.safe_url(node.url),
            hint=(
                "node is auth-gated but no storage_state is available. "
                "Complete the auth flow (see auth_session_* logs above) "
                "and rerun — extraction will capture the authenticated "
                "DOM and generate real tests."
            ),
        )
        return StageResult(
            node=node,
            extraction=None,
            pom_path=Path(node.pom_path) if node.pom_path else None,
            feature_path=Path(node.feature_path) if node.feature_path else None,
            steps_path=Path(node.steps_path) if node.steps_path else None,
            issues=["skipped: awaiting authenticated session"],
        )

    use_storage = (
        node.requires_auth
        or node.kind == URLKind.AUTHENTICATED
        or (auth_ready and node.kind != URLKind.LOGIN)
    )
    reason = (
        "requires_auth" if node.requires_auth
        else "kind=authenticated" if node.kind == URLKind.AUTHENTICATED
        else "auth_ready_session_reuse" if auth_ready
        else "anonymous"
    )
    logger.info(
        "extraction_storage_decision",
        slug=node.slug,
        use_storage=use_storage,
        reason=reason,
    )

    extraction, failure = _extract_detailed(node, settings, use_storage, shared=shared)
    if extraction is None and failure is not None:
        extraction = _maybe_escalate_to_auth(
            node, registry, settings, store, failure, use_storage, shared=shared
        )
    if extraction is None:
        node.status = Status.FAILED
        logger.error("url_failed", slug=node.slug, stage="extract")
        return StageResult(node=node, extraction=None, pom_path=None, feature_path=None, steps_path=None, issues=["extraction failed"])

    extraction.fingerprint = fingerprint_extraction(extraction)
    extraction_path = settings.paths.extractions_dir / f"{node.slug}.json"
    existed = extraction_path.exists()
    extraction_path.write_text(extraction.model_dump_json(indent=2), encoding="utf-8")
    node.extraction_path = str(extraction_path)
    node.status = Status.EXTRACTED
    logger.ok(
        "extraction_written",
        slug=node.slug,
        path=str(extraction_path),
        action="updated" if existed else "created",
        elements=len(extraction.elements),
        forms=len(extraction.forms),
        headings=len(extraction.headings),
        fingerprint=extraction.fingerprint,
    )

    prev_path = settings.paths.extractions_dir / f"{node.slug}.prev.json"
    prev = (
        PageExtraction.model_validate_json(prev_path.read_text(encoding="utf-8"))
        if prev_path.exists()
        else None
    )
    change = diff_extractions(prev, extraction)
    if prev is None:
        logger.info("diff_no_prev", slug=node.slug, reason="first_run_for_url")
    else:
        logger.info(
            "diff_report",
            slug=node.slug,
            added=len(change.added_elements),
            removed=len(change.removed_elements),
            selectors_changed=len(change.changed_selectors),
            title_changed=change.title_changed,
            headings_changed=change.headings_changed,
            needs_regen=change.needs_regeneration,
        )
    skip_regen = (
        not opts.force
        and node.last_fingerprint == extraction.fingerprint
        and not change.needs_regeneration
        and node.status == Status.COMPLETE
    )
    if skip_regen:
        logger.ok(
            "rerun_unchanged",
            slug=node.slug,
            url=logger.safe_url(node.url),
            fingerprint=extraction.fingerprint,
            reason="fingerprint_match",
        )
        return StageResult(
            node=node,
            extraction=extraction,
            pom_path=Path(node.pom_path) if node.pom_path else None,
            feature_path=Path(node.feature_path) if node.feature_path else None,
            steps_path=Path(node.steps_path) if node.steps_path else None,
            issues=issues,
        )

    if client is None:
        logger.warn("llm_skipped", slug=node.slug, reason="--skip-llm flag set")
        return StageResult(node=node, extraction=extraction, pom_path=None, feature_path=None, steps_path=None, issues=issues)

    # Empty extraction guard. An authenticated SPA that has not
    # hydrated will hand us zero interactive elements — if we still
    # call the LLM, it will hallucinate element ids ("title",
    # "response", "validation-error-message", ...) that do not exist
    # in ``SELECTORS``, and every generated step fails at runtime
    # with ``KeyError``. Refuse to generate in that case; the user
    # gets a clear reason and no broken tests on disk.
    if not extraction.elements:
        node.status = Status.NEEDS_IMPLEMENTATION
        note = "empty_extraction_skipping_generation"
        if note not in node.notes:
            node.notes.append(note)
        store.upsert_node(registry, node)
        logger.warn(
            "generation_skipped_empty_extraction",
            slug=node.slug,
            final_url=logger.safe_url(extraction.final_url),
            title=extraction.title,
            wait_strategy_hint=(
                "the page responded (HTTP 200) but no interactive "
                "elements rendered in time"
            ),
            hints=";".join([
                "increase EXTRACTION_NAV_TIMEOUT_MS in .env if the SPA is slow",
                "check that the URL is the correct post-auth landing (the "
                "authenticated landing page is often not the same as the "
                "marketing URL)",
                "delete .auth/user.json and re-auth if the session is stale",
            ]),
        )
        return StageResult(
            node=node,
            extraction=extraction,
            pom_path=None,
            feature_path=None,
            steps_path=None,
            issues=issues + ["skipped: extraction captured 0 interactive elements"],
        )

    page_class = page_class_name(node.slug)
    fixture_name = node.slug + "_page"

    logger.stage("pom_plan", slug=node.slug, fixture=fixture_name, elements=len(extraction.elements))
    pom_plan = generate_pom_plan(
        extraction,
        page_class=page_class,
        fixture_name=fixture_name,
        client=client,
        cache_dir=settings.paths.plans_dir,
        force=opts.force,
    )
    pom_path = settings.paths.pages_dir / f"{node.slug}_page.py"
    existed = pom_path.exists()
    pom_path.write_text(render_pom(pom_plan, extraction), encoding="utf-8")
    node.pom_path = str(pom_path)
    node.status = Status.POM_READY
    logger.ok(
        "pom_written",
        slug=node.slug,
        path=str(pom_path),
        action="updated" if existed else "created",
        methods=len(pom_plan.methods),
    )

    logger.stage(
        "feature_plan",
        slug=node.slug,
        tiers=",".join(opts.tiers),
        pom_methods=len(pom_plan.methods),
    )
    feature_plan = generate_feature_plan(
        extraction,
        pom_plan=pom_plan,
        requested_tiers=opts.tiers,
        client=client,
        cache_dir=settings.paths.plans_dir,
        force=opts.force,
    )
    feature_path = settings.paths.features_dir / f"{node.slug}.feature"
    existed = feature_path.exists()
    feature_path.write_text(render_feature(feature_plan), encoding="utf-8")
    node.feature_path = str(feature_path)
    node.status = Status.FEATURE_READY
    logger.ok(
        "feature_written",
        slug=node.slug,
        path=str(feature_path),
        action="updated" if existed else "created",
        scenarios=len(feature_plan.scenarios),
        background_steps=len(feature_plan.background),
    )

    steps_path = settings.paths.steps_dir / f"test_{node.slug}.py"
    rel_feature = feature_path.relative_to(settings.paths.features_dir)
    existed = steps_path.exists()
    rendered_steps = render_steps(
        feature_title=feature_plan.feature,
        feature_path=str(rel_feature).replace("\\", "/"),
        feature_plan=feature_plan,
        pom_plan=pom_plan,
        pom_module=f"{node.slug}_page",
        elements=list(extraction.elements),
    )
    # Syntax-check the rendered module before writing. A single bad
    # step — e.g. a string argument joined as a bare identifier —
    # turns the whole file into a ``SyntaxError`` that blocks pytest
    # collection for ALL tests, not just this slug's. When that
    # happens we rewrite the offending bodies to
    # ``NotImplementedError`` so collection stays clean and the
    # auto-heal pass can fill them.
    import ast as _ast

    try:
        _ast.parse(rendered_steps)
    except SyntaxError as exc:
        logger.warn(
            "steps_syntax_error",
            slug=node.slug,
            err=str(exc),
            line=exc.lineno or 0,
            hint=(
                "rewriting the whole module's step bodies to "
                "NotImplementedError so pytest collection succeeds; "
                "auto-heal will refill them via the LLM."
            ),
        )
        rendered_steps = _strip_step_bodies_for_heal(rendered_steps)
        # If stripping somehow produces another SyntaxError, refuse to
        # write — better to leave the previous good file in place.
        try:
            _ast.parse(rendered_steps)
        except SyntaxError as exc2:
            logger.error(
                "steps_write_aborted",
                slug=node.slug,
                err=str(exc2),
                hint="fallback strip also produced invalid Python; not writing",
            )
            node.status = Status.FAILED
            return StageResult(
                node=node,
                extraction=extraction,
                pom_path=pom_path,
                feature_path=feature_path,
                steps_path=None,
                issues=issues + [f"steps_syntax_error_unrecoverable: {exc2!s}"],
            )
    steps_path.write_text(rendered_steps, encoding="utf-8")
    node.steps_path = str(steps_path)
    node.last_fingerprint = extraction.fingerprint

    placeholder_count = rendered_steps.count("NotImplementedError")
    if placeholder_count > 0 and client is not None:
        logger.stage(
            "steps_autoheal",
            slug=node.slug,
            placeholders=placeholder_count,
            hint=(
                "recommended scenarios produced assertion/nav steps that "
                "could not be deterministically bound; asking the LLM to "
                "fill each stub so every recommended test actually runs"
            ),
        )
        try:
            from autocoder.heal import HealOptions, heal_steps

            heal_results = heal_steps(settings, HealOptions(slug=node.slug))
            applied = sum(1 for h in heal_results if h.applied)
            placeholder_count = steps_path.read_text(encoding="utf-8").count(
                "NotImplementedError"
            )
            logger.ok(
                "steps_autoheal_done",
                slug=node.slug,
                stubs=len(heal_results),
                applied=applied,
                remaining_placeholders=placeholder_count,
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "steps_autoheal_failed",
                slug=node.slug,
                err=str(exc),
                err_type=type(exc).__name__,
            )

    if placeholder_count > 0:
        node.status = Status.NEEDS_IMPLEMENTATION
        issues.append(
            f"steps_placeholders={placeholder_count} (test file will fail until bound)"
        )
        logger.warn(
            "steps_incomplete",
            slug=node.slug,
            path=str(steps_path),
            placeholder_count=placeholder_count,
            hint=(
                "Some step texts could not be bound to POM methods, "
                "synthesized automatically, or healed by the LLM. Inspect "
                "the placeholder bodies in the generated test file."
            ),
        )
    else:
        node.status = Status.COMPLETE

    logger.ok(
        "steps_written",
        slug=node.slug,
        path=str(steps_path),
        action="updated" if existed else "created",
        status=node.status.value,
        placeholders=placeholder_count,
    )

    extraction_path.replace(prev_path)
    extraction_path.write_text(extraction.model_dump_json(indent=2), encoding="utf-8")

    return StageResult(
        node=node,
        extraction=extraction,
        pom_path=pom_path,
        feature_path=feature_path,
        steps_path=steps_path,
        issues=issues,
    )


def _maybe_escalate_to_auth(
    node: URLNode,
    registry: Registry,
    settings: Settings,
    store: RegistryStore,
    failure: dict,
    already_used_storage: bool,
    *,
    shared: BrowserSession | None = None,
) -> PageExtraction | None:
    """Convert an anonymous extract failure into an auth-first retry.

    The core rule: if a URL could not be reached anonymously and there
    is *any* evidence that authentication is the reason, the system
    must run the login flow before giving up on the URL.

    Evidence we accept:

    * ``kind == "auth_blocked"`` — goto landed on a login-shaped URL.
    * ``kind == "auth_unreachable"`` and either (a) the redirect chain
      touched a login-shaped URL, (b) a ``LOGIN_URL`` is configured, or
      (c) a login URL was already discovered during classification.

    Returns the new :class:`PageExtraction` on success, or ``None`` if
    we could not escalate.
    """
    if already_used_storage:
        return None  # we already tried with a session; do not loop.

    kind = failure.get("kind", "other")
    redirects = failure.get("redirects") or []
    final_url = failure.get("final_url") or ""

    evidence = (
        kind == "auth_blocked"
        or any(looks_like_login_url(u) for u in redirects)
        or looks_like_login_url(final_url)
        or bool(settings.login_url)
        or any(
            n.kind in (URLKind.LOGIN, URLKind.REDIRECT_TO_LOGIN)
            for n in registry.nodes.values()
        )
    )
    if not evidence:
        return None

    # Mark the node as needing auth so the registry reflects reality.
    node.requires_auth = True
    if kind == "auth_blocked" and final_url:
        node.redirects_to = final_url
        if node.kind == URLKind.UNKNOWN:
            node.kind = URLKind.REDIRECT_TO_LOGIN
    store.upsert_node(registry, node)

    if registry.auth is None:
        # Prefer the redirect target as the login URL if we saw one;
        # otherwise fall through to the normal seeding logic.
        discovered = next(
            (u for u in redirects if looks_like_login_url(u)),
            final_url if looks_like_login_url(final_url) else None,
        )
        registry.auth = _maybe_seed_auth(registry, settings, discovered)
        if registry.auth is None:
            logger.warn(
                "auth_escalation_no_login_url",
                slug=node.slug,
                hint="Set LOGIN_URL in .env — cannot run auth-first without it.",
            )
            return None
        store.save(registry)

    if registry.auth.status != Status.STEPS_READY:
        logger.info(
            "auth_escalation_materialise",
            slug=node.slug,
            login_url=logger.safe_url(registry.auth.login_url),
        )
        _materialise_auth(registry, settings, store)

    if not _storage_state_usable(settings):
        logger.warn(
            "auth_escalation_no_storage",
            slug=node.slug,
            hint=(
                "Run `pytest tests/auth_setup -m auth_setup` to populate "
                "storage_state, then rerun."
            ),
        )
        return None

    logger.info("auth_escalation_retry", slug=node.slug, use_storage=True)
    retry, retry_failure = _extract_detailed(node, settings, use_storage=True, shared=shared)
    if retry is not None:
        logger.ok("auth_escalation_succeeded", slug=node.slug)
        return retry
    logger.error(
        "auth_escalation_failed",
        slug=node.slug,
        err=(retry_failure or {}).get("err", "unknown"),
    )
    return None


def _is_session_alive(shared: BrowserSession | None) -> bool:
    """Cheap probe to check whether the shared Playwright session is
    still usable. Reads ``page.url`` — if the context, browser, or
    page was closed behind our back, Playwright raises and we return
    False.
    """
    if shared is None or shared.page is None:
        return False
    try:
        _ = shared.page.url  # noqa: F841  — intentional probe
    except Exception:
        return False
    try:
        return not shared.page.is_closed()
    except Exception:
        return False


def _ensure_shared_alive(
    shared: BrowserSession | None,
    settings: Settings,
    registry: Registry,
) -> BrowserSession | None:
    """Return a live shared session; reopen if the existing one died.

    The typical failure mode is: the pipeline extracts URL #1, spends
    12-15 minutes running POM / feature / heal LLM calls (all CPU-
    bound, no browser work), then comes back to extract URL #2 and
    finds the Chromium window closed. This helper transparently
    reopens the browser context from the still-valid storage_state on
    disk so the remaining URLs don't all fail with "browser has been
    closed".
    """
    if _is_session_alive(shared):
        return shared
    if registry.auth is None:
        # No auth spec → no storage_state to restore. The initial
        # shared context was opened without it; reopening the same way
        # is fine.
        logger.warn(
            "shared_session_died_reopening",
            reason="context_closed_between_urls",
            with_storage=False,
        )
        return open_shared_session(settings, use_storage_state=False).__enter__()
    logger.warn(
        "shared_session_died_reopening",
        reason="context_closed_between_urls",
        with_storage=True,
        hint=(
            "reopening with storage_state so MSAL cookies + localStorage "
            "are restored; silent_reauth will rehydrate sessionStorage "
            "on the first extraction"
        ),
    )
    try:
        new_sess = open_shared_session(settings, use_storage_state=True).__enter__()
        return new_sess
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "shared_session_reopen_failed",
            err=str(exc)[:200],
            hint=(
                "could not reopen the browser; remaining URLs will skip. "
                "Delete .auth/user.json and rerun if session was stale."
            ),
        )
        return None


def _storage_state_usable(settings: Settings) -> bool:
    sp = settings.paths.storage_state
    try:
        return sp.exists() and sp.stat().st_size > 0
    except OSError:
        return False


def _settle_after_auth(
    shared: BrowserSession | None,
    settings: Settings,
    post_auth_url: str,
) -> None:
    """Move the shared browser OFF the SSO return URL onto a real
    authenticated route before extraction starts.

    Why: many enterprise MSAL apps register ``/login`` (or similar)
    as the OAuth ``redirect_uri``, even though that route does not
    exist in the SPA router. The redirect lands the tab on a 404
    shell while MSAL tokens sit in ``sessionStorage`` unread. If the
    per-URL extraction pipeline starts from that tab, the SPA's
    first navigation to a protected route can bounce straight back
    to ``/login`` (because MSAL never got a chance to mount on a
    real page), and the extractor captures the 404 DOM as if it were
    the real application.

    The fix is to navigate ONCE to a known-real route (``base_url``)
    and wait for the page to leave the ``/login`` shell. After that,
    every subsequent ``goto_resilient`` starts from a hydrated,
    authenticated SPA. The authenticated-shell quick-check uses the
    same DOM heuristic as the silent-reauth path so we stop at the
    first sign of a real app UI.

    No-op when:
    * no shared session was opened (the runner used a throwaway
      context),
    * ``settings.base_url`` is empty,
    * the post-auth URL is already on ``base_url`` and off ``/login``.
    """
    if shared is None:
        return
    target = (settings.base_url or "").rstrip("/")
    # Fallback when .env has no BASE_URL: derive the origin from the
    # post-auth URL. For ``https://app.example.com/login?code=x`` that
    # gives ``https://app.example.com`` — exactly what we want to nav
    # to so the SPA boots on a real route instead of the 404 /login.
    if not target and post_auth_url:
        try:
            from urllib.parse import urlparse as _urlparse
            p = _urlparse(post_auth_url)
            if p.scheme and p.netloc:
                target = f"{p.scheme}://{p.netloc}"
                logger.info(
                    "auth_settle_target_from_post_auth",
                    target=logger.safe_url(target),
                    hint=(
                        "BASE_URL is empty in .env; using the origin of "
                        "the post-auth URL as the settle target."
                    ),
                )
        except Exception:
            pass
    if not target:
        logger.warn(
            "auth_settle_skipped",
            reason="no_target_url",
            hint="set BASE_URL in .env so settle-after-auth can nav off the OAuth return URL",
        )
        return
    cur = post_auth_url or ""
    if target in cur and "/login" not in cur:
        logger.info(
            "auth_settle_skipped",
            reason="post_auth_url_already_on_base",
            url=logger.safe_url(cur),
        )
        return

    logger.info(
        "auth_settle_start",
        from_url=logger.safe_url(cur),
        to_url=logger.safe_url(target),
        hint=(
            "moving the shared browser off the OAuth return URL onto "
            "base_url so MSAL hydrates on a real SPA route; protects "
            "extraction from the '/login 404' redirect bounce."
        ),
    )
    try:
        goto_resilient(
            shared.page,
            target,
            nav_timeout_ms=settings.browser.extraction_nav_timeout_ms,
            diagnostics_dir=settings.paths.logs_dir,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warn(
            "auth_settle_nav_failed",
            err=str(exc)[:200],
            hint=(
                "couldn't settle onto base_url — extraction will retry "
                "per-URL with the same storage_state anyway"
            ),
        )
        return

    # Wait for a real SPA element to attach, giving MSAL time to
    # process whatever was in sessionStorage. 15 s is enough even on
    # slow CPUs.
    try:
        shared.page.wait_for_selector(
            'button, a[href], input, textarea, select, '
            '[role="button"], [role="link"], [role="textbox"]',
            state="attached",
            timeout=15_000,
        )
    except Exception:
        pass

    # If the app's auth shell is still showing (SSO button visible),
    # try a silent re-auth on this fresh tab. Cheap: the tokens are
    # already in storage, so it completes without MFA.
    try:
        if _looks_auth_gated_quick(shared.page):
            logger.info(
                "auth_settle_silent_reauth",
                hint="base_url still shows the SSO shell; triggering silent MSAL handshake",
            )
            _silent_reauth(shared, target, settings)
    except Exception as exc:  # noqa: BLE001
        logger.warn("auth_settle_silent_reauth_failed", err=str(exc)[:200])

    try:
        final = shared.page.url or ""
    except Exception:
        final = ""
    logger.ok(
        "auth_settle_done",
        final_url=logger.safe_url(final),
        on_login_shell="/login" in final,
        hint=(
            "extraction pipeline now starts from this URL instead of the "
            "OAuth return landing"
        ),
    )


_AUTH_GATED_SSO_PHRASES = (
    "sign in with microsoft",
    "continue with microsoft",
    "sign in with google",
    "continue with google",
    "sign in with github",
    "sign in with sso",
    "single sign-on",
)


def _looks_auth_gated_quick(page) -> bool:
    """Fast DOM check: is the page showing a visible SSO provider button?

    Mirrors the classifier's ``_looks_auth_gated`` but keeps the scan
    cheap enough to run on every extraction. ``True`` means "the SPA
    is sitting on a pre-auth consent/gateway shell" — the caller
    should attempt silent re-auth before enumerating elements.
    """
    import re as _re

    for phrase in _AUTH_GATED_SSO_PHRASES:
        rx = _re.compile(_re.escape(phrase), _re.IGNORECASE)
        for role in ("button", "link"):
            try:
                loc = page.get_by_role(role, name=rx)
                if loc.count() == 0:
                    continue
                h = loc.first.element_handle()
                if h and h.is_visible():
                    return True
            except Exception:
                continue
    return False


def _silent_reauth(sess, target_url: str, settings: Settings) -> bool:
    """Trigger MSAL's silent sign-in path in the current browser context.

    Procedure
    ---------
    1. Find the visible SSO button on the current page.
    2. If it is disabled (consent-checkbox gate), tick visible
       unchecked checkboxes to enable it — same logic the auth
       runner already uses to unblock a disabled provider button.
    3. Click the button. Because the Entra tenant cookies were
       restored from ``storage_state``, the redirect completes
       without a password/MFA prompt (`/oauth2/authorize` returns a
       redirect back to the app within a few seconds).
    4. Wait up to 60s for the page URL to leave
       ``login.microsoftonline.com`` and return inside ``base_url``.
    5. Re-navigate to the originally-requested URL so extraction
       happens against authenticated content.

    Returns ``True`` when the page leaves the consent shell; ``False``
    when the handshake did not complete (stored tokens expired,
    tenant requires interactive MFA, or the button could not be
    clicked).
    """
    from autocoder.extract.auth_runner import _unblock_sso_button
    from autocoder.extract.browser import goto_resilient

    page = sess.page
    # Find the provider button. We don't care which provider — the
    # first visible SSO-phrased button is the right one.
    btn = None
    import re as _re

    for phrase in _AUTH_GATED_SSO_PHRASES:
        rx = _re.compile(_re.escape(phrase), _re.IGNORECASE)
        for role in ("button", "link"):
            try:
                loc = page.get_by_role(role, name=rx)
                if loc.count() == 0:
                    continue
                h = loc.first.element_handle()
                if h and h.is_visible():
                    btn = loc.first
                    break
            except Exception:
                continue
        if btn is not None:
            break
    if btn is None:
        return False

    # If the button is gated behind a consent checkbox, try to
    # unblock it. We reuse the auth runner's helper — feeding it a
    # minimal fake StableSelector isn't worth it; instead we just
    # tick visible checkboxes directly here.
    try:
        if not btn.is_enabled():
            for sel in (
                'input[type="checkbox"]:not(:checked)',
                '[role="checkbox"][aria-checked="false"]',
            ):
                try:
                    cbs = page.locator(sel)
                    for i in range(min(cbs.count(), 5)):
                        cb = cbs.nth(i)
                        if not cb.is_visible():
                            continue
                        try:
                            cb.check(timeout=2_000)
                        except Exception:
                            try:
                                cb.click(timeout=2_000)
                            except Exception:
                                continue
                        if btn.is_enabled():
                            break
                except Exception:
                    continue
                if btn.is_enabled():
                    break
    except Exception:
        pass

    try:
        btn.click(timeout=10_000)
    except Exception as exc:
        logger.warn("silent_reauth_click_failed", err=str(exc))
        return False

    # Wait for the page to leave the IdP and return to the app.
    entra = "login.microsoftonline.com"
    app_origin = settings.base_url or target_url
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        try:
            url = page.url or ""
        except Exception:
            url = ""
        if entra not in url and "/login" not in url and url and url != "about:blank":
            break
        try:
            page.wait_for_timeout(500)
        except Exception:
            time.sleep(0.5)
    else:
        return False

    # Re-navigate to the original target so extraction works
    # against the authenticated DOM.
    try:
        goto_resilient(
            page,
            target_url,
            nav_timeout_ms=settings.browser.extraction_nav_timeout_ms,
            diagnostics_dir=settings.paths.logs_dir,
        )
    except Exception as exc:
        logger.warn("silent_reauth_renav_failed", err=str(exc))
        return False

    # Give the SPA a moment to render its authenticated layout, then
    # confirm the consent shell is gone.
    try:
        page.wait_for_selector(
            'button, a[href], input, textarea, select, [role="button"]',
            state="attached",
            timeout=15_000,
        )
    except Exception:
        pass
    return not _looks_auth_gated_quick(page)


@contextmanager
def _session_or_open(
    shared: BrowserSession | None,
    settings: Settings,
    *,
    use_storage: bool,
):
    """Yield ``shared`` if provided, else spin up a one-shot ``open_session``.

    Lets the auth runner and every extract call use the same page
    when the orchestrator is running the authenticated lifecycle in a
    shared long-lived browser, while keeping the classifier /
    homepage-probe paths on the current throwaway-context path (they
    run anonymously anyway).
    """
    if shared is not None:
        yield shared
    else:
        with open_session(settings, use_storage_state=use_storage) as sess:
            yield sess


def _extract(node: URLNode, settings: Settings, use_storage: bool) -> PageExtraction | None:
    """Extract one URL. Returns ``None`` on any failure.

    For callers that need to distinguish *authentication-blocked*
    failures from generic ones, see :func:`_extract_with_escalation`.
    """
    result, _ = _extract_detailed(node, settings, use_storage)
    return result


def _extract_detailed(
    node: URLNode,
    settings: Settings,
    use_storage: bool,
    *,
    shared: BrowserSession | None = None,
) -> tuple[PageExtraction | None, dict | None]:
    """Return ``(extraction, failure_info)``.

    ``failure_info`` is ``None`` on success. On failure it contains
    diagnostics the orchestrator uses to decide whether to escalate
    into the auth-first flow:

    * ``kind``: ``"auth_unreachable"`` | ``"auth_blocked"`` | ``"other"``
    * ``final_url``: last URL Playwright saw (post-redirect).
    * ``redirects``: the redirect chain captured by the nav probe.
    * ``err``: string form of the original error.
    """
    started = time.monotonic()
    logger.info(
        "extract_start",
        slug=node.slug,
        url=logger.safe_url(node.url),
        use_storage=use_storage,
        shared=shared is not None,
    )
    try:
        with _session_or_open(shared, settings, use_storage=use_storage) as sess:
            try:
                diag = goto_resilient(
                    sess.page,
                    node.url,
                    nav_timeout_ms=settings.browser.extraction_nav_timeout_ms,
                    diagnostics_dir=settings.paths.logs_dir,
                )
            except AuthUnreachable as exc:
                logger.error(
                    "extract_failed",
                    slug=node.slug,
                    url=logger.safe_url(node.url),
                    err=str(exc),
                    kind="auth_unreachable",
                    duration=f"{time.monotonic() - started:.2f}s",
                    **exc.diag.to_dict(),
                )
                return None, {
                    "kind": "auth_unreachable",
                    "final_url": exc.diag.final_url,
                    "redirects": list(exc.diag.redirects),
                    "err": str(exc),
                }

            # Detect "we loaded something, but it is actually the login
            # page" — very common when a protected URL is hit without
            # a valid session.
            final_url = diag.final_url or sess.page.url
            if (
                not use_storage
                and final_url
                and final_url != node.url
                and looks_like_login_url(final_url)
            ):
                logger.warn(
                    "extract_redirected_to_login",
                    slug=node.slug,
                    url=logger.safe_url(node.url),
                    final_url=logger.safe_url(final_url),
                )
                return None, {
                    "kind": "auth_blocked",
                    "final_url": final_url,
                    "redirects": list(diag.redirects),
                    "err": "redirected_to_login",
                }

            # Settle wait: many authenticated SPAs only commit the HTML
            # shell inside the nav-timeout budget and rely on XHR +
            # client-side rendering for the real DOM. If ``commit`` was
            # the only wait tier that succeeded, the page is almost
            # certainly still hydrating. Give it a bounded additional
            # window to produce at least one interactive element before
            # we enumerate. Without this, ``extract_page`` runs against
            # an empty shell and downstream stages hallucinate.
            if "domcontentloaded" not in diag.wait_strategy:
                try:
                    sess.page.wait_for_selector(
                        'button, a[href], input, textarea, select, '
                        '[role="button"], [role="link"], [role="textbox"]',
                        state="attached",
                        timeout=15_000,
                    )
                    logger.info(
                        "extract_settle_succeeded",
                        slug=node.slug,
                        hint="at least one interactive element attached",
                    )
                except Exception:
                    logger.warn(
                        "extract_settle_timeout",
                        slug=node.slug,
                        url=logger.safe_url(node.url),
                        hint=(
                            "no interactive element attached within 15s after "
                            "commit. The page may still be rendering, may be "
                            "genuinely empty, or may be blocked by a network "
                            "dependency. Extraction will still run but is "
                            "likely to return 0 elements."
                        ),
                    )

            # Silent re-auth for MSAL-style SPAs. Many single-page apps
            # that use MSAL.js keep their authenticated account in
            # ``sessionStorage`` -- which Playwright's ``storage_state``
            # does NOT persist across contexts (only cookies and
            # localStorage are saved). So even with a valid session
            # file, hitting ``/catalog`` directly in a fresh context
            # often shows the pre-auth consent shell because the SPA
            # can't find an MSAL account.
            #
            # Recovery: if (a) we are running under storage_state and
            # (b) the page we landed on still shows the SSO entry
            # shell, click the provider button once. Because the
            # Entra tenant cookies ARE in storage_state, MSAL will
            # complete the handshake silently (no MFA re-prompt),
            # populate sessionStorage, and the SPA will transition to
            # the authenticated view. We then re-navigate to the
            # originally-requested URL so the extraction runs against
            # real content.
            # Bounce detection: even if the SSO shell isn't visible
            # via the quick check, a URL mismatch between ``node.url``
            # and the post-goto final_url — where the final URL looks
            # login-shaped — almost always means "the SPA's auth
            # guard redirected us because MSAL hasn't hydrated yet".
            # Force the silent-reauth path in that case too, not just
            # on visual DOM evidence.
            bounced_to_login = (
                use_storage
                and final_url
                and node.url
                and final_url.rstrip("/") != node.url.rstrip("/")
                and looks_like_login_url(final_url)
            )
            needs_reauth = (
                use_storage
                and (bounced_to_login or _looks_auth_gated_quick(sess.page))
            )
            if needs_reauth:
                logger.info(
                    "extract_silent_reauth_attempt",
                    slug=node.slug,
                    bounced=bounced_to_login,
                    final_url=logger.safe_url(final_url or ""),
                    hint=(
                        "page either bounced to a login-shaped URL or "
                        "still shows the SSO consent shell after loading "
                        "storage_state; clicking the provider button to "
                        "let MSAL hydrate sessionStorage"
                    ),
                )
                if _silent_reauth(sess, node.url, settings):
                    logger.ok(
                        "extract_silent_reauth_succeeded",
                        slug=node.slug,
                        final_url=logger.safe_url(sess.page.url),
                    )
                else:
                    logger.warn(
                        "extract_silent_reauth_failed",
                        slug=node.slug,
                        final_url=logger.safe_url(sess.page.url),
                        hint=(
                            "silent MSAL handshake did not complete within "
                            "60s. The stored session may be expired, or "
                            "the tenant requires a fresh MFA. Delete "
                            ".auth/user.json and rerun to re-capture."
                        ),
                    )

            extraction = extract_page(
                sess.page,
                url=node.url,
                settings=settings,
                requires_auth=node.requires_auth,
                kind=node.kind,
            )
            logger.ok(
                "extract_done",
                slug=node.slug,
                final_url=logger.safe_url(extraction.final_url),
                title=extraction.title[:60],
                elements=len(extraction.elements),
                wait_strategy=diag.wait_strategy,
                duration=f"{time.monotonic() - started:.2f}s",
            )
            return extraction, None
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "extract_failed",
            slug=node.slug,
            url=logger.safe_url(node.url),
            err=str(exc),
            kind="other",
            duration=f"{time.monotonic() - started:.2f}s",
        )
        return None, {
            "kind": "other",
            "final_url": "",
            "redirects": [],
            "err": str(exc),
        }


# ---------------------------------------------------------------------------
# Extension hook
# ---------------------------------------------------------------------------


def run_extend(settings: Settings, urls: Iterable[str], extra_tiers: list[str]) -> list[StageResult]:
    """Add scenarios for new tiers to URLs already in the registry.

    Implementation note: the extension is just a normal generate run
    with ``force=True`` and an enlarged tier list, so the same plan
    cache + diff logic applies. The orchestrator never duplicates
    scenarios because the feature plan validator dedupes by title.
    """
    store = RegistryStore(settings.paths.registry_path)
    registry = store.load()
    target_urls = list(urls) or list(registry.nodes.keys())
    tiers = sorted(set(DEFAULT_TIERS + extra_tiers))
    logger.info(
        "extend_resolved",
        urls=len(target_urls),
        tiers=",".join(tiers),
        source="provided" if urls else "entire_registry",
    )
    return run_generate(settings, GenerateOptions(urls=target_urls, tiers=tiers, force=True))
