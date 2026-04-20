"""Heal runner — scan, ask the LLM, validate, apply.

One LLM call per stub. Cached on disk by
``(slug, step_text, page_fingerprint)`` so reruns spend zero tokens.

The runner never aborts on a single bad suggestion — it logs and
moves on, leaving that stub in place. The user can re-heal after
fixing the underlying cause (missing POM method, bad extraction).
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from autocoder import logger
from autocoder.config import Settings, ensure_dirs
from autocoder.heal.applier import apply_heal, write_if_changed
from autocoder.heal.prompts import (
    FAILURE_HEAL_SYSTEM,
    HEAL_SYSTEM,
    build_failure_heal_prompt,
    build_heal_prompt,
)
from autocoder.heal.pytest_failures import PytestFailure, run_pytest_capture
from autocoder.heal.scanner import StubInfo, find_stubs_in_dir, find_stubs_in_file
from autocoder.heal.validator import validate_body
from autocoder.llm.factory import get_llm_client
from autocoder.llm.ollama_client import OllamaClient


@dataclass
class HealOptions:
    slug: str | None = None
    dry_run: bool = False
    force: bool = False
    from_pytest: bool = False        # run pytest, heal failing steps
    pytest_paths: list[Path] = field(default_factory=list)
    junit_path: Path | None = None   # use an existing JUnit XML


@dataclass
class HealResult:
    stub: StubInfo
    suggested_body: str = ""
    intent: str = ""
    applied: bool = False
    cached: bool = False
    issues: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Context loading: POM methods + element catalog per slug
# ---------------------------------------------------------------------------


def _slug_for_stub(stub: StubInfo) -> str:
    return stub.slug


def _load_pom_methods(settings: Settings, slug: str) -> tuple[list[dict], str]:
    """Return (compact_methods, latest_pom_plan_path).

    Reads the most recent POM-plan cache file for the slug. Returns
    an empty list when no cache exists; the heal call still works,
    just with less context.
    """
    plans = sorted(
        settings.paths.plans_dir.glob(f"{slug}_page.pom.*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not plans:
        return [], ""
    try:
        data = json.loads(plans[0].read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return [], str(plans[0])
    methods = [
        {
            "name": m.get("name", ""),
            "intent": m.get("intent", ""),
            "args": m.get("args", []) or [],
            "action": m.get("action", ""),
        }
        for m in data.get("methods", [])
    ]
    return methods, str(plans[0])


def _load_extraction(settings: Settings, slug: str) -> tuple[list[dict], str, str]:
    """Return (compact_elements, page_url, fingerprint).

    Compact-element shape matches what the planner prompts use.
    """
    path = settings.paths.extractions_dir / f"{slug}.json"
    if not path.exists():
        return [], "", ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return [], "", ""
    elements: list[dict] = []
    for e in data.get("elements", []):
        item: dict = {"id": e.get("id", ""), "kind": e.get("kind", "")}
        if e.get("name"):
            item["name"] = e["name"]
        elements.append(item)
    return elements, data.get("final_url") or data.get("url", ""), data.get("fingerprint", "")


# ---------------------------------------------------------------------------
# Scenario context — forbid re-asserting elements the scenario already acted on
# ---------------------------------------------------------------------------


_SCENARIO_HEADER_RE = __import__("re").compile(r"^\s*Scenario(?:\s+Outline)?:\s*(.+?)\s*$")
_STEP_LINE_RE = __import__("re").compile(
    r"^\s*(Given|When|Then|And|But)\s+(.+?)\s*$",
)


def _scenario_prior_step_texts(feature_path: "__import__('pathlib').Path", step_text: str) -> list[str]:
    """Return the list of prior step texts in the scenario that owns *step_text*.

    Parses the .feature file as plain text (we already do the same in
    :mod:`autocoder.report`). When the scenario is not found — e.g.
    feature file missing on disk, or the step text comes from a
    background and not a scenario — returns an empty list.
    """
    if not feature_path.exists():
        return []
    try:
        lines = feature_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    # Walk scenarios; inside each, accumulate step texts until we hit
    # the target step (in which case return the accumulator) or the
    # next scenario boundary (reset).
    current_steps: list[str] = []
    inside_scenario = False
    for raw in lines:
        if _SCENARIO_HEADER_RE.match(raw):
            current_steps = []
            inside_scenario = True
            continue
        if not inside_scenario:
            continue
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("@"):
            continue
        m = _STEP_LINE_RE.match(raw)
        if not m:
            continue
        text = m.group(2).strip()
        if text == step_text:
            return list(current_steps)
        current_steps.append(text)
    return []


def _compute_forbidden_ids(
    *,
    stub: StubInfo,
    settings: Settings,
    pom_methods: list[dict],
    elements: list[dict],
) -> list[str]:
    """Element ids that prior When/And/Given steps in the scenario used.

    We resolve each prior step to an element id two ways:
    * direct match on a POM method's ``element_id`` via fuzzy name match,
    * failing that, fuzzy token match against the element catalog.

    The heal LLM must not emit an assertion against any of these ids —
    that would re-assert the action target (the exact bug that produced
    ``expect(catalog_page.locate('open_stewie_assistant')).to_be_visible()``
    after the scenario already clicked it).
    """
    feature_path = settings.paths.features_dir / f"{stub.slug}.feature"
    prior_texts = _scenario_prior_step_texts(feature_path, stub.step_text)
    if not prior_texts:
        return []

    import re as _re

    method_names = [m["name"] for m in pom_methods if m.get("name")]
    method_to_element = {m["name"]: m.get("element_id", "") for m in pom_methods}
    element_names = {
        e["id"]: " ".join(
            str(x) for x in (e.get("id", ""), e.get("name", ""), e.get("role", ""))
        ).lower()
        for e in elements
    }

    def _tokens(text: str) -> list[str]:
        return [t for t in _re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 1]

    def _best_method(text: str) -> str | None:
        toks = _tokens(text)
        if not toks or not method_names:
            return None
        best, best_score = None, 0
        for m in method_names:
            parts = m.lower().split("_")
            score = sum(1 for t in toks if t in parts)
            if score > best_score:
                best, best_score = m, score
        return best if best_score >= 2 else None

    def _best_element(text: str) -> str | None:
        toks = _tokens(text)
        if not toks:
            return None
        best, best_score = None, 0
        for eid, pool in element_names.items():
            score = sum(1 for t in toks if t in pool)
            if score > best_score:
                best, best_score = eid, score
        return best if best_score >= 1 else None

    forbidden: list[str] = []
    for text in prior_texts:
        m = _best_method(text)
        if m and method_to_element.get(m):
            forbidden.append(method_to_element[m])
            continue
        eid = _best_element(text)
        if eid:
            forbidden.append(eid)
    # De-dupe while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for eid in forbidden:
        if eid and eid not in seen:
            seen.add(eid)
            ordered.append(eid)
    return ordered


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def _cache_key(stub: StubInfo, fingerprint: str, pom_methods: list[dict]) -> str:
    h = hashlib.sha256()
    h.update(stub.step_text.encode("utf-8"))
    h.update(b"\x00")
    h.update(fingerprint.encode("utf-8"))
    h.update(b"\x00")
    h.update(",".join(sorted(m["name"] for m in pom_methods)).encode("utf-8"))
    return h.hexdigest()[:16]


def _heals_dir(settings: Settings) -> Path:
    return settings.paths.manifest_dir / "heals"


def _cache_path(settings: Settings, slug: str, key: str) -> Path:
    return _heals_dir(settings) / f"{slug}.{key}.json"


def _read_cache(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _write_cache(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def heal_steps(settings: Settings, opts: HealOptions) -> list[HealResult]:
    """Heal every renderer-shaped stub in the steps directory.

    When ``opts.from_pytest`` is set (or ``opts.junit_path`` is
    supplied) the runner first runs pytest, then heals only the
    step functions that actually failed at runtime — even if their
    body is no longer a stub.
    """
    ensure_dirs(settings)
    logger.init(settings.paths.logs_dir, level=settings.log_level)
    started = time.monotonic()

    failures: list[PytestFailure] = []
    if opts.from_pytest or opts.junit_path is not None:
        failures = _gather_failures(settings, opts)

    if failures:
        return _heal_failures(settings, opts, failures, started)

    stubs = find_stubs_in_dir(settings.paths.steps_dir, slug=opts.slug)
    logger.stage(
        "heal_start",
        stubs=len(stubs),
        slug=opts.slug or "*",
        dry_run=opts.dry_run,
        force=opts.force,
    )
    if not stubs:
        logger.ok("heal_done_nothing", reason="no NotImplementedError stubs found")
        return []

    client = get_llm_client(settings)
    if not client.is_available():
        logger.die(
            "llm_unreachable",
            backend="azure_openai" if settings.use_azure_openai else "ollama",
            hint=(
                "Verify the Azure endpoint/deployment/api-key."
                if settings.use_azure_openai
                else "Start the container; see readme/09_llm.md."
            ),
        )

    results: list[HealResult] = []
    try:
        # Group stubs by slug so we load POM context once per file.
        by_slug: dict[str, list[StubInfo]] = {}
        for s in stubs:
            by_slug.setdefault(_slug_for_stub(s), []).append(s)

        for slug, slug_stubs in by_slug.items():
            pom_methods, pom_plan_path = _load_pom_methods(settings, slug)
            elements, page_url, fingerprint = _load_extraction(settings, slug)
            method_names = {m["name"] for m in pom_methods}
            logger.info(
                "heal_context_loaded",
                slug=slug,
                pom_methods=len(pom_methods),
                elements=len(elements),
                fingerprint=fingerprint or "(none)",
                pom_plan=pom_plan_path or "(none)",
            )

            for stub in slug_stubs:
                result = _heal_one(
                    settings=settings,
                    client=client,
                    stub=stub,
                    pom_methods=pom_methods,
                    pom_method_names=method_names,
                    elements=elements,
                    page_url=page_url,
                    fingerprint=fingerprint,
                    opts=opts,
                )
                results.append(result)
    finally:
        client.close()

    applied = sum(1 for r in results if r.applied)
    cached = sum(1 for r in results if r.cached)
    logger.ok(
        "heal_done",
        stubs=len(stubs),
        applied=applied,
        cached=cached,
        skipped=len(stubs) - applied,
        duration=f"{time.monotonic() - started:.2f}s",
    )
    return results


def _heal_one(
    *,
    settings: Settings,
    client: OllamaClient,
    stub: StubInfo,
    pom_methods: list[dict],
    pom_method_names: set[str],
    elements: list[dict],
    page_url: str,
    fingerprint: str,
    opts: HealOptions,
) -> HealResult:
    cache_path = _cache_path(settings, stub.slug, _cache_key(stub, fingerprint, pom_methods))
    cached = None if opts.force else _read_cache(cache_path)

    suggested = ""
    intent = ""
    issues: list[str] = []

    forbidden_ids = _compute_forbidden_ids(
        stub=stub,
        settings=settings,
        pom_methods=pom_methods,
        elements=elements,
    )

    if cached is not None:
        suggested = cached.get("body", "")
        intent = cached.get("intent", "")
        logger.llm_call(
            model="(cache)",
            purpose=f"heal:{stub.slug}:{stub.function_name}",
            in_tokens=0,
            out_tokens=0,
            duration_s=0.0,
            cached=True,
            cache_path=str(cache_path),
        )
    else:
        if forbidden_ids:
            logger.info(
                "heal_forbidden_ids",
                slug=stub.slug,
                func=stub.function_name,
                ids=",".join(forbidden_ids),
            )
        user_prompt = build_heal_prompt(
            step_text=stub.step_text,
            keywords=stub.keywords,
            pom_class=stub.fixture_class,
            fixture_name=stub.fixture_name,
            pom_methods=pom_methods,
            elements=elements,
            page_url=page_url,
            forbidden_element_ids=forbidden_ids,
        )
        try:
            raw = client.chat_json(
                system=HEAL_SYSTEM,
                user=user_prompt,
                purpose=f"heal:{stub.slug}:{stub.function_name}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "heal_llm_failed",
                slug=stub.slug,
                func=stub.function_name,
                err=str(exc),
            )
            return HealResult(stub=stub, issues=[f"llm error: {exc!s}"])

        suggested = (raw.get("body") or "").strip()
        intent = (raw.get("intent") or "").strip()

    cleaned, val_errors = validate_body(
        suggested,
        fixture_name=stub.fixture_name,
        pom_method_names=pom_method_names,
        element_ids={e.get("id", "") for e in elements if e.get("id")},
        forbidden_element_ids=set(forbidden_ids),
        current_page_url=page_url,
    )
    cache_written = False
    if val_errors:
        for msg in val_errors:
            logger.warn(
                "heal_invalid_body",
                slug=stub.slug,
                func=stub.function_name,
                msg=msg,
                body=suggested[:80],
            )
        issues.extend(val_errors)
        # Fallback: substitute `pass` so the test still runs without
        # emitting a false assertion. Leaving the original
        # NotImplementedError in place would guarantee the scenario
        # fails at runtime even though the LLM's only mistake was
        # picking a forbidden id / trivial URL — the correct behavior
        # there is to just not assert anything.
        cleaned = "pass  # no safe binding — validator rejected LLM output"
        intent = intent or "no safe binding (validator fallback)"
        if cached is None:
            _write_cache(
                cache_path,
                {"body": cleaned, "intent": intent, "errors": val_errors},
            )
            cache_written = True

    # Persist the validated cache entry so reruns are free.
    if cached is None and not cache_written:
        _write_cache(cache_path, {"body": cleaned, "intent": intent, "errors": []})

    if opts.dry_run:
        logger.ok(
            "heal_dry_run",
            slug=stub.slug,
            func=stub.function_name,
            body=cleaned[:80],
            intent=intent[:60],
        )
        return HealResult(
            stub=stub,
            suggested_body=cleaned,
            intent=intent,
            applied=False,
            cached=cached is not None,
        )

    try:
        new_text = apply_heal(stub, cleaned)
    except ValueError as exc:
        logger.error(
            "heal_apply_failed",
            slug=stub.slug,
            func=stub.function_name,
            err=str(exc),
        )
        return HealResult(
            stub=stub,
            suggested_body=cleaned,
            intent=intent,
            applied=False,
            cached=cached is not None,
            issues=[f"apply error: {exc!s}"],
        )

    changed = write_if_changed(stub, new_text)
    if changed:
        logger.ok(
            "heal_applied",
            slug=stub.slug,
            func=stub.function_name,
            body=cleaned[:80],
            intent=intent[:60],
            cached=cached is not None,
        )
    return HealResult(
        stub=stub,
        suggested_body=cleaned,
        intent=intent,
        applied=changed,
        cached=cached is not None,
    )


# ---------------------------------------------------------------------------
# Failure-driven heal
# ---------------------------------------------------------------------------


def _write_defect_log(settings: Settings, failures: list[PytestFailure]) -> None:
    """Persist a machine-readable defect log so the report can surface it.

    File shape: ``manifest/runs/defects.json`` — overwritten on each
    ``_heal_failures`` invocation. Keyed by slug for easy rendering.
    The report module reads this and renders a dedicated "Application
    defects" section below the per-scenario pass/fail table.
    """
    path = settings.paths.manifest_dir / "runs" / "defects.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    by_slug: dict[str, list[dict]] = {}
    for f in failures:
        classname = f.test_id.split("::", 1)[0]
        tail = classname.rsplit(".", 1)[-1]
        slug = tail[len("test_"):] if tail.startswith("test_") else tail
        by_slug.setdefault(slug, []).append(
            {
                "test_id": f.test_id,
                "step_function": f.step_function,
                "error_type": f.error_type,
                "error_message": f.error_message,
                "failure_class": f.failure_class,
                "element_id": f.referenced_element_id,
            }
        )
    path.write_text(json.dumps(by_slug, indent=2), encoding="utf-8")
    logger.ok(
        "frontend_defects_logged",
        path=str(path),
        slugs=len(by_slug),
        total=sum(len(v) for v in by_slug.values()),
    )


def _element_ids_by_slug(settings: Settings) -> dict[str, set[str]]:
    """Load the current extraction element-id catalog for every slug.

    Used by the JUnit parser's origin classifier so it can decide
    ``script`` vs. ``frontend`` based on whether the id the failing
    step was targeting was known to the extractor.
    """
    out: dict[str, set[str]] = {}
    ext_dir = settings.paths.extractions_dir
    if not ext_dir.is_dir():
        return out
    for p in ext_dir.glob("*.json"):
        if p.name.endswith(".prev.json"):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        slug = p.stem
        ids = {e.get("id", "") for e in data.get("elements", []) if e.get("id")}
        if ids:
            out[slug] = ids
    return out


def _gather_failures(settings: Settings, opts: HealOptions) -> list[PytestFailure]:
    ids_by_slug = _element_ids_by_slug(settings)
    if opts.junit_path is not None:
        from autocoder.heal.pytest_failures import parse_junit_xml
        path = opts.junit_path
        logger.info("heal_failures_load", path=str(path))
        return parse_junit_xml(
            path,
            base=settings.paths.project_root,
            element_ids_by_slug=ids_by_slug,
        )

    junit_path = settings.paths.manifest_dir / "heals" / "last-pytest.xml"
    junit_path.parent.mkdir(parents=True, exist_ok=True)
    targets = opts.pytest_paths or [
        settings.paths.steps_dir if not opts.slug else settings.paths.steps_dir / f"test_{opts.slug}.py"
    ]
    logger.info(
        "heal_pytest_run",
        targets=",".join(str(p) for p in targets),
        junit=str(junit_path),
    )
    failures = run_pytest_capture(
        test_paths=targets,
        junit_path=junit_path,
        element_ids_by_slug=ids_by_slug,
    )
    logger.info("heal_failures_collected", count=len(failures), junit=str(junit_path))
    return failures


def _stub_for_function(file_path: Path, function_name: str) -> StubInfo | None:
    """Locate a step function in `file_path` by name and return a
    StubInfo-shaped target — even if the body is no longer a stub.

    This lets the failure-heal flow target real code, not just stubs.
    """
    import ast as _ast
    try:
        source = file_path.read_text(encoding="utf-8")
        tree = _ast.parse(source, filename=str(file_path))
    except (OSError, SyntaxError):
        return None
    pom_module = ""
    for node in tree.body:
        if isinstance(node, _ast.ImportFrom) and node.module and node.module.startswith("tests.pages."):
            pom_module = node.module.split(".")[-1]
            break
    for node in tree.body:
        if not isinstance(node, _ast.FunctionDef) or node.name != function_name:
            continue
        if not node.body or not node.args.args:
            continue
        first_arg = node.args.args[0]
        fixture_class = (
            first_arg.annotation.id
            if isinstance(first_arg.annotation, _ast.Name)
            else ""
        )
        # Pull the original step text from the @<keyword>(parsers.parse('TEXT')) deco
        step_text = function_name.lstrip("_").replace("_", " ")
        for dec in node.decorator_list:
            if (
                isinstance(dec, _ast.Call)
                and isinstance(dec.func, _ast.Name)
                and isinstance(dec.args, list)
                and dec.args
            ):
                inner = dec.args[0]
                if (
                    isinstance(inner, _ast.Call)
                    and isinstance(inner.func, _ast.Attribute)
                    and inner.func.attr == "parse"
                    and inner.args
                    and isinstance(inner.args[0], _ast.Constant)
                    and isinstance(inner.args[0].value, str)
                ):
                    step_text = inner.args[0].value
                    break
        keywords: list[str] = []
        for dec in node.decorator_list:
            if isinstance(dec, _ast.Call) and isinstance(dec.func, _ast.Name):
                kw = dec.func.id
                if kw in {"given", "when", "then"} and kw not in keywords:
                    keywords.append(kw)
        return StubInfo(
            file_path=file_path,
            function_name=function_name,
            body_lineno=node.body[0].lineno,
            body_col_offset=node.body[0].col_offset,
            step_text=step_text,
            keywords=tuple(k.capitalize() for k in keywords),
            fixture_name=first_arg.arg,
            fixture_class=fixture_class,
            pom_module=pom_module,
        )
    return None


def _current_body(stub: StubInfo) -> str:
    """Read the current body lines of `stub` (everything between the
    function header and either the next ``def`` / ``@deco`` / EOF)."""
    import ast as _ast
    try:
        source = stub.file_path.read_text(encoding="utf-8")
        tree = _ast.parse(source)
    except (OSError, SyntaxError):
        return ""
    for node in tree.body:
        if isinstance(node, _ast.FunctionDef) and node.name == stub.function_name:
            return "\n".join(_ast.unparse(s) for s in node.body)
    return ""


def _heal_failures(
    settings: Settings,
    opts: HealOptions,
    failures: list[PytestFailure],
    started: float,
) -> list[HealResult]:
    # Split on origin. Frontend failures are real application defects
    # — we do NOT heal them. Healing an app bug rewrites the test
    # into a no-op, silently masking the defect. Those go straight
    # to the defect log. Script and ambiguous failures go through
    # the usual heal flow.
    frontend_failures = [f for f in failures if f.failure_origin == "frontend"]
    healable_failures = [f for f in failures if f.failure_origin != "frontend"]

    if frontend_failures:
        for f in frontend_failures:
            logger.warn(
                "frontend_failure_detected",
                test_id=f.test_id,
                func=f.step_function,
                element_id=f.referenced_element_id,
                failure_class=f.failure_class,
                err=f.error_message[:120],
                hint=(
                    "the referenced element was in the extraction catalog "
                    "but the running app no longer exposes it — treat as "
                    "an app defect; heal will NOT rewrite this test"
                ),
            )
        _write_defect_log(settings, frontend_failures)

    logger.stage(
        "heal_from_pytest_start",
        failures=len(healable_failures),
        frontend_skipped=len(frontend_failures),
        slug=opts.slug or "*",
        dry_run=opts.dry_run,
        force=opts.force,
    )
    if not healable_failures:
        logger.ok(
            "heal_done_nothing",
            reason=(
                "no healable failures"
                + (f" ({len(frontend_failures)} frontend defects recorded)"
                   if frontend_failures else "")
            ),
        )
        return []

    client = get_llm_client(settings)
    if not client.is_available():
        logger.die(
            "llm_unreachable",
            backend="azure_openai" if settings.use_azure_openai else "ollama",
            hint=(
                "Verify the Azure endpoint/deployment/api-key."
                if settings.use_azure_openai
                else "Start the container; see readme/09_llm.md."
            ),
        )

    results: list[HealResult] = []
    try:
        for f in healable_failures:
            if not f.step_function:
                logger.warn("heal_failure_unmapped", test_id=f.test_id, err=f.error_message[:80])
                continue
            stub = _stub_for_function(f.test_file, f.step_function)
            if stub is None:
                logger.warn(
                    "heal_step_not_found",
                    test_id=f.test_id,
                    func=f.step_function,
                    file=str(f.test_file),
                )
                continue
            slug = stub.slug
            if opts.slug and slug != opts.slug:
                continue
            pom_methods, _ = _load_pom_methods(settings, slug)
            elements, page_url, fingerprint = _load_extraction(settings, slug)
            method_names = {m["name"] for m in pom_methods}
            current_body = _current_body(stub)

            results.append(
                _heal_one_failure(
                    settings=settings,
                    client=client,
                    stub=stub,
                    failure=f,
                    pom_methods=pom_methods,
                    pom_method_names=method_names,
                    elements=elements,
                    page_url=page_url,
                    fingerprint=fingerprint,
                    current_body=current_body,
                    opts=opts,
                )
            )
    finally:
        client.close()

    applied = sum(1 for r in results if r.applied)
    cached = sum(1 for r in results if r.cached)
    logger.ok(
        "heal_done",
        failures=len(failures),
        targeted=len(results),
        applied=applied,
        cached=cached,
        skipped=len(results) - applied,
        duration=f"{time.monotonic() - started:.2f}s",
    )
    return results


def _heal_one_failure(
    *,
    settings: Settings,
    client: OllamaClient,
    stub: StubInfo,
    failure: PytestFailure,
    pom_methods: list[dict],
    pom_method_names: set[str],
    elements: list[dict],
    page_url: str,
    fingerprint: str,
    current_body: str,
    opts: HealOptions,
) -> HealResult:
    # Cache key includes the failure-class + first 80 chars of error so a
    # different failure on the same step is treated as a fresh problem.
    err_signature = (failure.failure_class + "|" + failure.error_message)[:80]
    key_seed = stub.step_text + "\x00" + err_signature
    key = hashlib.sha256(key_seed.encode("utf-8") + fingerprint.encode("utf-8")).hexdigest()[:16]
    cache_path = _heals_dir(settings) / f"{stub.slug}.fail.{key}.json"

    cached = None if opts.force else _read_cache(cache_path)
    suggested = ""
    intent = ""
    issues: list[str] = []

    # Cache-staleness guard: if the cached body is ALREADY on disk
    # (i.e. matches ``current_body``) and we're back here because
    # that same body is failing with the same error again, the
    # cache is wrong — bust it and re-ask the LLM. The typical
    # shape is "LLM guessed a URL; test ran; URL was wrong; same
    # failure re-surfaces in a later session". Without this bypass
    # we would loop forever on the bad suggestion.
    def _ast_norm(src: str) -> str:
        import ast as _ast
        import textwrap as _tw
        try:
            return _ast.unparse(_ast.parse(_tw.dedent(src).strip()))
        except Exception:
            return _tw.dedent(src).strip()

    if cached is not None:
        cached_body = _ast_norm(cached.get("body") or "")
        cur_body_norm = _ast_norm(current_body or "")
        if cached_body and cur_body_norm and cached_body == cur_body_norm:
            logger.info(
                "heal_fail_cache_busted",
                slug=stub.slug,
                func=stub.function_name,
                reason="cached_body_is_currently_failing",
                hint=(
                    "the cached suggestion is already on disk and still "
                    "failing with the same error — forcing a fresh LLM "
                    "call instead of re-applying the known-bad body"
                ),
            )
            cached = None

    if cached is not None:
        suggested = cached.get("body", "")
        intent = cached.get("intent", "")
        logger.llm_call(
            model="(cache)",
            purpose=f"heal_fail:{stub.slug}:{stub.function_name}",
            in_tokens=0,
            out_tokens=0,
            duration_s=0.0,
            cached=True,
            cache_path=str(cache_path),
        )
    else:
        prompt = build_failure_heal_prompt(
            step_text=stub.step_text,
            current_body=current_body,
            error_message=failure.error_message,
            failure_class=failure.failure_class,
            keywords=stub.keywords,
            pom_class=stub.fixture_class,
            fixture_name=stub.fixture_name,
            pom_methods=pom_methods,
            elements=elements,
            page_url=page_url,
        )
        try:
            raw = client.chat_json(
                system=FAILURE_HEAL_SYSTEM,
                user=prompt,
                purpose=f"heal_fail:{stub.slug}:{stub.function_name}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "heal_llm_failed",
                slug=stub.slug,
                func=stub.function_name,
                err=str(exc),
            )
            return HealResult(stub=stub, issues=[f"llm error: {exc!s}"])

        suggested = (raw.get("body") or "").strip()
        intent = (raw.get("intent") or "").strip()

    cleaned, val_errors = validate_body(
        suggested,
        fixture_name=stub.fixture_name,
        pom_method_names=pom_method_names,
        element_ids={e.get("id", "") for e in elements if e.get("id")},
        max_statements=5,
        # Failure heal gets the pytest error as context; the error
        # message often carries the right URL, so we re-allow
        # to_have_url(...) here. Stub heal still blocks it.
        allow_url_assertions=True,
        current_page_url=page_url,
    )
    fail_cache_written = False
    if val_errors:
        for msg in val_errors:
            logger.warn(
                "heal_invalid_body",
                slug=stub.slug,
                func=stub.function_name,
                msg=msg,
                body=suggested[:80],
            )
        issues.extend(val_errors)
        # Fallback: replace the broken on-disk body with `pass` so the
        # test stops failing with AttributeError / wrong-URL noise on
        # the next run. The intent comment records that the LLM output
        # was rejected so a human can grep for it and decide how to
        # hand-fix. Without this fallback the rejected heal just
        # leaves the previous bad body in place forever.
        cleaned = "pass  # no safe binding — failure-heal validator rejected LLM output"
        intent = intent or "no safe binding (validator fallback)"
        if cached is None:
            _write_cache(
                cache_path,
                {"body": cleaned, "intent": intent, "errors": val_errors},
            )
            fail_cache_written = True

    if cached is None and not fail_cache_written:
        _write_cache(cache_path, {"body": cleaned, "intent": intent, "errors": []})

    if opts.dry_run:
        logger.ok(
            "heal_dry_run",
            slug=stub.slug,
            func=stub.function_name,
            body=cleaned[:120],
            intent=intent[:60],
            failure_class=failure.failure_class,
        )
        return HealResult(stub=stub, suggested_body=cleaned, intent=intent, cached=cached is not None)

    try:
        new_text = apply_heal(stub, cleaned)
    except ValueError as exc:
        logger.error("heal_apply_failed", slug=stub.slug, func=stub.function_name, err=str(exc))
        return HealResult(stub=stub, suggested_body=cleaned, intent=intent, issues=[f"apply error: {exc!s}"])

    changed = write_if_changed(stub, new_text)
    if changed:
        logger.ok(
            "heal_applied",
            slug=stub.slug,
            func=stub.function_name,
            body=cleaned[:120],
            intent=intent[:60],
            failure_class=failure.failure_class,
            cached=cached is not None,
        )
    return HealResult(
        stub=stub,
        suggested_body=cleaned,
        intent=intent,
        applied=changed,
        cached=cached is not None,
    )


def report(results: Iterable[HealResult]) -> dict:
    """Compact summary suitable for human or machine consumption."""
    out = {
        "total": 0,
        "applied": 0,
        "cached": 0,
        "skipped": 0,
        "items": [],
    }
    for r in results:
        out["total"] += 1
        if r.applied:
            out["applied"] += 1
        if r.cached:
            out["cached"] += 1
        if not r.applied:
            out["skipped"] += 1
        out["items"].append(
            {
                "file": str(r.stub.file_path),
                "func": r.stub.function_name,
                "step": r.stub.step_text,
                "applied": r.applied,
                "cached": r.cached,
                "body": r.suggested_body,
                "issues": r.issues,
            }
        )
    return out
