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
from autocoder.extract.browser import open_session
from autocoder.extract.inspector import extract_page
from autocoder.generate.auth_setup import render_auth_setup
from autocoder.generate.feature import render_feature
from autocoder.generate.pom import render_pom
from autocoder.generate.steps import render_steps
from autocoder.intake.classifier import classify_urls
from autocoder.intake.graph import build_dependency_graph, topological_order
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
    logger.init(settings.paths.runs_log, level=settings.log_level)
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

    # Stage 2: auth-first — establish AuthSpec if anything needs it
    needs_auth = any(n.requires_auth for n in registry.nodes.values())
    logger.stage("auth_first", needs_auth=needs_auth)
    if registry.auth is None:
        registry.auth = _maybe_seed_auth(registry, settings, detected_login)
    if registry.auth is not None:
        _materialise_auth(registry, settings, store)
    else:
        logger.info("auth_skipped", reason="no_authenticated_urls")

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

    client: OllamaClient | None = None
    if not opts.skip_llm:
        client = OllamaClient(settings.ollama)
        logger.info(
            "ollama_check",
            endpoint=settings.ollama.endpoint,
            model=settings.ollama.model,
        )
        if not client.is_available():
            logger.die(
                "ollama_unreachable",
                endpoint=settings.ollama.endpoint,
                hint="Start the container; see readme/09_llm.md.",
            )
        logger.ok("ollama_ready", endpoint=settings.ollama.endpoint, model=settings.ollama.model)
    else:
        logger.warn("llm_skipped_by_flag", flag="--skip-llm")

    results: list[StageResult] = []
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

            try:
                result = _process_url(node, registry, settings, client, opts)
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
    logger.ok(
        "run_done",
        processed=len(results),
        duration=f"{time.monotonic() - run_started:.2f}s",
    )
    return results


def run_status(settings: Settings) -> Registry:
    store = RegistryStore(settings.paths.registry_path)
    return store.load()


# ---------------------------------------------------------------------------
# Auth-first
# ---------------------------------------------------------------------------


def _maybe_seed_auth(
    registry: Registry,
    settings: Settings,
    detected_login: str | None,
) -> AuthSpec | None:
    needs_auth = any(n.requires_auth for n in registry.nodes.values())
    if not needs_auth:
        return None
    login_url = None
    source = ""
    if settings.login_url:
        login_url, source = settings.login_url, "env:LOGIN_URL"
    elif detected_login:
        login_url, source = detected_login, "classifier_detection"
    if not login_url:
        for n in registry.nodes.values():
            if n.kind == URLKind.LOGIN:
                login_url, source = n.url, "input_url_list"
                break
    if not login_url:
        logger.warn(
            "auth_needed_but_no_login_url",
            hint="Set LOGIN_URL in .env or include the login URL in the input list.",
        )
        return None
    logger.info(
        "auth_seeded",
        login_url=logger.safe_url(login_url),
        source=source,
        storage_state=str(settings.paths.storage_state),
    )
    return AuthSpec(login_url=login_url, storage_state_path=str(settings.paths.storage_state))


def _materialise_auth(
    registry: Registry,
    settings: Settings,
    store: RegistryStore,
) -> None:
    """Probe the login page, render the auth setup test, persist the spec."""
    auth = registry.auth
    if auth is None:
        return
    setup_path = settings.paths.auth_setup_dir / "test_auth_setup.py"
    if auth.status == Status.STEPS_READY and setup_path.exists():
        logger.info(
            "auth_setup_reused",
            path=str(setup_path),
            reason="status=steps_ready and file exists",
        )
        return

    logger.info("auth_probe_start", url=logger.safe_url(auth.login_url))
    with open_session(settings, use_storage_state=False) as sess:
        try:
            sess.page.goto(auth.login_url, wait_until="domcontentloaded")
        except Exception as exc:  # noqa: BLE001
            logger.warn("auth_probe_failed", url=logger.safe_url(auth.login_url), err=str(exc))
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
            hint="Login page may use SSO redirect or non-standard markup.",
        )
        return
    logger.ok(
        "auth_form_detected",
        url=logger.safe_url(auth.login_url),
        username_strategy=seeded.username_selector.strategy.value if seeded.username_selector else "?",
        password_strategy=seeded.password_selector.strategy.value if seeded.password_selector else "?",
        submit_strategy=seeded.submit_selector.strategy.value if seeded.submit_selector else "?",
        username_env_present=settings.secret_present("LOGIN_USERNAME"),
        password_env_present=settings.secret_present("LOGIN_PASSWORD"),
    )

    merged = auth.model_copy(
        update={
            "username_selector": seeded.username_selector,
            "password_selector": seeded.password_selector,
            "submit_selector": seeded.submit_selector,
            "success_indicator_url_contains": seeded.success_indicator_url_contains,
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


# ---------------------------------------------------------------------------
# Per-URL pipeline
# ---------------------------------------------------------------------------


def _process_url(
    node: URLNode,
    registry: Registry,
    settings: Settings,
    client: OllamaClient | None,
    opts: GenerateOptions,
) -> StageResult:
    issues: list[str] = []
    use_storage = node.requires_auth or node.kind == URLKind.AUTHENTICATED
    logger.info(
        "extraction_storage_decision",
        slug=node.slug,
        use_storage=use_storage,
        reason="requires_auth" if node.requires_auth else (
            "kind=authenticated" if node.kind == URLKind.AUTHENTICATED else "anonymous"
        ),
    )

    extraction = _extract(node, settings, use_storage)
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
    steps_path.write_text(
        render_steps(
            feature_title=feature_plan.feature,
            feature_path=str(rel_feature).replace("\\", "/"),
            feature_plan=feature_plan,
            pom_plan=pom_plan,
            pom_module=f"{node.slug}_page",
        ),
        encoding="utf-8",
    )
    node.steps_path = str(steps_path)
    node.status = Status.COMPLETE
    node.last_fingerprint = extraction.fingerprint
    logger.ok(
        "steps_written",
        slug=node.slug,
        path=str(steps_path),
        action="updated" if existed else "created",
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


def _extract(node: URLNode, settings: Settings, use_storage: bool) -> PageExtraction | None:
    started = time.monotonic()
    logger.info(
        "extract_start",
        slug=node.slug,
        url=logger.safe_url(node.url),
        use_storage=use_storage,
    )
    try:
        with open_session(settings, use_storage_state=use_storage) as sess:
            sess.page.goto(node.url, wait_until="domcontentloaded")
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
                duration=f"{time.monotonic() - started:.2f}s",
            )
            return extraction
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "extract_failed",
            slug=node.slug,
            url=logger.safe_url(node.url),
            err=str(exc),
            duration=f"{time.monotonic() - started:.2f}s",
        )
        return None


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
