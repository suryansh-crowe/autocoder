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

    client = OllamaClient(settings.ollama)
    if not client.is_available():
        logger.die(
            "ollama_unreachable",
            endpoint=settings.ollama.endpoint,
            hint="Start the container; see readme/09_llm.md.",
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
        user_prompt = build_heal_prompt(
            step_text=stub.step_text,
            keywords=stub.keywords,
            pom_class=stub.fixture_class,
            fixture_name=stub.fixture_name,
            pom_methods=pom_methods,
            elements=elements,
            page_url=page_url,
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
    )
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
        # Still cache the raw response so the user can inspect it; the
        # cache stores both body + the validation errors so a rerun
        # without --force does not re-spend tokens on a known-bad output.
        if cached is None:
            _write_cache(
                cache_path,
                {"body": suggested, "intent": intent, "errors": val_errors},
            )
        return HealResult(
            stub=stub,
            suggested_body=suggested,
            intent=intent,
            applied=False,
            cached=cached is not None,
            issues=issues,
        )

    # Persist the validated cache entry so reruns are free.
    if cached is None:
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


def _gather_failures(settings: Settings, opts: HealOptions) -> list[PytestFailure]:
    if opts.junit_path is not None:
        from autocoder.heal.pytest_failures import parse_junit_xml
        path = opts.junit_path
        logger.info("heal_failures_load", path=str(path))
        return parse_junit_xml(path, base=settings.paths.project_root)

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
    failures = run_pytest_capture(test_paths=targets, junit_path=junit_path)
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
    logger.stage(
        "heal_from_pytest_start",
        failures=len(failures),
        slug=opts.slug or "*",
        dry_run=opts.dry_run,
        force=opts.force,
    )
    if not failures:
        logger.ok("heal_done_nothing", reason="pytest reported no failures")
        return []

    client = OllamaClient(settings.ollama)
    if not client.is_available():
        logger.die(
            "ollama_unreachable",
            endpoint=settings.ollama.endpoint,
            hint="Start the container; see readme/09_llm.md.",
        )

    results: list[HealResult] = []
    try:
        for f in failures:
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
    )
    if val_errors:
        for msg in val_errors:
            logger.warn(
                "heal_invalid_body",
                slug=stub.slug,
                func=stub.function_name,
                msg=msg,
                body=suggested[:80],
            )
        if cached is None:
            _write_cache(cache_path, {"body": suggested, "intent": intent, "errors": val_errors})
        return HealResult(
            stub=stub,
            suggested_body=suggested,
            intent=intent,
            applied=False,
            cached=cached is not None,
            issues=val_errors,
        )

    if cached is None:
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
