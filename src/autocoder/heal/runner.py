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
from autocoder.heal.prompts import HEAL_SYSTEM, build_heal_prompt
from autocoder.heal.scanner import StubInfo, find_stubs_in_dir
from autocoder.heal.validator import validate_body
from autocoder.llm.ollama_client import OllamaClient


@dataclass
class HealOptions:
    slug: str | None = None
    dry_run: bool = False
    force: bool = False


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
    """Heal every renderer-shaped stub in the steps directory."""
    ensure_dirs(settings)
    logger.init(settings.paths.runs_log, level=settings.log_level)
    started = time.monotonic()

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
