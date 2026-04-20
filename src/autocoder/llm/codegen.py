"""LLM-driven generation of pure-Playwright pytest scripts (the 3rd prompt).

Takes a validated :class:`PageExtraction` + :class:`POMPlan` +
:class:`FeaturePlan` and produces a :class:`PlaywrightScriptPlan`:
one pytest test function per scenario, each body a list of Python
statements that call the POM fixture or Playwright primitives.

Cached on disk by ``(slug, feature_fingerprint)`` so reruns on
unchanged pages cost zero tokens.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from autocoder import logger
from autocoder.llm.ollama_client import OllamaError
from autocoder.llm.prompts import (
    PLAYWRIGHT_CODEGEN_SYSTEM,
    build_playwright_codegen_prompt,
)
from autocoder.models import (
    FeaturePlan,
    PageExtraction,
    PlaywrightScriptPlan,
    PlaywrightTest,
    POMPlan,
)


_ALLOWED_TIERS = {
    "smoke",
    "sanity",
    "regression",
    "happy",
    "edge",
    "validation",
    "navigation",
    "auth",
    "rbac",
    "e2e",
}


def _feature_fingerprint(feature_plan: FeaturePlan) -> str:
    """Stable short hash over scenario titles + step texts + tiers."""
    h = hashlib.sha256()
    for sc in feature_plan.scenarios:
        h.update(sc.title.encode("utf-8"))
        h.update(sc.tier.encode("utf-8"))
        for st in sc.steps:
            h.update(st.text.encode("utf-8"))
    return h.hexdigest()[:12]


def _read_cache(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.warn("codegen_cache_corrupt", path=str(path), err=str(exc))
        return None


def _write_cache(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _fallback_plan(
    feature_plan: FeaturePlan,
    pom_plan: POMPlan,
    extraction: PageExtraction,
    pom_module: str,
    err: str,
) -> PlaywrightScriptPlan:
    """Minimal plan used when the LLM cannot produce valid JSON.

    One placeholder test per scenario. Each body is just ``pom.navigate()``
    so the renderer still produces a runnable file. The orchestrator
    counts the placeholders and flags the slug as NEEDS_IMPLEMENTATION.
    """
    tests: list[PlaywrightTest] = []
    for idx, sc in enumerate(feature_plan.scenarios, start=1):
        safe = (
            "test_"
            + "".join(c if c.isalnum() else "_" for c in sc.title.lower())
            .strip("_")[:50]
        ) or f"test_scenario_{idx}"
        tests.append(
            PlaywrightTest(
                name=safe,
                scenario_title=sc.title,
                tier=sc.tier if sc.tier in _ALLOWED_TIERS else "smoke",
                tags=[sc.tier if sc.tier in _ALLOWED_TIERS else "smoke"],
                statements=["pom.navigate()"],
            )
        )
    logger.warn(
        "playwright_plan_fallback",
        fixture=pom_plan.fixture_name,
        scenarios=len(feature_plan.scenarios),
        err=err[:120],
    )
    return PlaywrightScriptPlan(
        url=extraction.final_url,
        pom_class=pom_plan.class_name,
        pom_fixture=pom_plan.fixture_name,
        pom_module=pom_module,
        tests=tests,
    )


def _validate_raw(
    raw: dict,
    *,
    pom_method_names: set[str],
) -> tuple[list[PlaywrightTest], list[str]]:
    issues: list[str] = []
    out: list[PlaywrightTest] = []
    tests = raw.get("tests")
    if not isinstance(tests, list) or not tests:
        issues.append("missing 'tests' list")
        return out, issues

    seen_names: set[str] = set()
    for i, entry in enumerate(tests):
        if not isinstance(entry, dict):
            issues.append(f"tests[{i}] is not an object")
            continue
        name = str(entry.get("name") or "").strip()
        if not name.startswith("test_"):
            name = "test_" + name.lstrip("_")
        name = "".join(c if (c.isalnum() or c == "_") else "_" for c in name)[:60]
        if not name or name == "test_":
            issues.append(f"tests[{i}] has empty name")
            continue
        base = name
        suffix = 2
        while name in seen_names:
            name = f"{base}_{suffix}"
            suffix += 1
        seen_names.add(name)

        tier = str(entry.get("tier") or "smoke").strip().lower()
        if tier not in _ALLOWED_TIERS:
            issues.append(f"tests[{i}] unknown tier {tier!r}, defaulting to smoke")
            tier = "smoke"
        tags_raw = entry.get("tags") or [tier]
        tags = [str(t).strip() for t in tags_raw if str(t).strip()] or [tier]
        if tags[0] != tier:
            tags = [tier] + [t for t in tags if t != tier]

        stmts_raw = entry.get("statements") or []
        if not isinstance(stmts_raw, list):
            issues.append(f"tests[{i}] statements is not a list")
            continue
        statements = [str(s).strip() for s in stmts_raw if str(s).strip()]
        if not statements:
            statements = ["pass"]
        if statements[0] != "pom.navigate()":
            statements = ["pom.navigate()"] + statements

        out.append(
            PlaywrightTest(
                name=name,
                scenario_title=str(entry.get("scenario_title") or name),
                tier=tier,
                tags=tags,
                statements=statements,
            )
        )
    return out, issues


def generate_playwright_script_plan(
    extraction: PageExtraction,
    *,
    feature_plan: FeaturePlan,
    pom_plan: POMPlan,
    pom_module: str,
    client,
    cache_dir: Path,
    force: bool = False,
) -> PlaywrightScriptPlan:
    """Call the LLM once to produce a validated :class:`PlaywrightScriptPlan`."""
    fp = _feature_fingerprint(feature_plan)
    cache_path = (
        cache_dir
        / f"{pom_plan.fixture_name}.playwright.{extraction.fingerprint}.{fp}.json"
    )
    cached = None if force else _read_cache(cache_path)
    if cached is not None:
        logger.llm_call(
            model="(cache)",
            purpose=f"playwright_plan:{pom_plan.fixture_name}",
            in_tokens=0,
            out_tokens=0,
            duration_s=0.0,
            cached=True,
            cache_path=str(cache_path),
        )
        logger.info(
            "playwright_plan_cache_hit",
            fixture=pom_plan.fixture_name,
            fingerprint=extraction.fingerprint,
            feature_fp=fp,
            path=str(cache_path),
        )
        return PlaywrightScriptPlan(**cached)

    if force:
        logger.info("playwright_plan_cache_skipped", fixture=pom_plan.fixture_name, reason="--force")
    else:
        logger.info(
            "playwright_plan_cache_miss",
            fixture=pom_plan.fixture_name,
            fingerprint=extraction.fingerprint,
            feature_fp=fp,
        )

    user = build_playwright_codegen_prompt(
        extraction,
        feature_plan=feature_plan,
        pom_plan=pom_plan,
    )
    try:
        raw = client.chat_json(
            system=PLAYWRIGHT_CODEGEN_SYSTEM,
            user=user,
            purpose=f"playwright_plan:{pom_plan.fixture_name}",
        )
    except OllamaError as exc:
        return _fallback_plan(feature_plan, pom_plan, extraction, pom_module, str(exc))

    method_names = {m.name for m in pom_plan.methods}
    tests, issues = _validate_raw(raw, pom_method_names=method_names)
    if issues:
        logger.warn(
            "playwright_plan_validator",
            fixture=pom_plan.fixture_name,
            issues=len(issues),
        )
        for msg in issues:
            logger.warn("playwright_plan_issue", fixture=pom_plan.fixture_name, msg=msg)
    if not tests:
        return _fallback_plan(
            feature_plan, pom_plan, extraction, pom_module, "no valid tests in LLM output"
        )

    plan = PlaywrightScriptPlan(
        url=extraction.final_url,
        pom_class=pom_plan.class_name,
        pom_fixture=pom_plan.fixture_name,
        pom_module=pom_module,
        tests=tests,
    )
    _write_cache(cache_path, plan.model_dump(mode="json"))
    logger.ok(
        "playwright_plan_validated",
        fixture=pom_plan.fixture_name,
        tests=len(tests),
    )
    return plan
