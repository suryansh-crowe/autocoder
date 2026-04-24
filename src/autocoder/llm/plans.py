"""High-level helpers that take an extraction and return a validated plan.

These are the only call sites in the orchestrator that reach the LLM.
Both helpers cache their output to ``manifest/plans/`` keyed by the
extraction fingerprint, so reruns on unchanged pages do zero LLM work.

Every entry/exit point logs:

* whether a cached plan was reused (and from which file),
* tokens consumed by the LLM call (via :func:`logger.llm_call`),
* validator issues per plan, with explanations.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from autocoder import logger
from autocoder.llm.ollama_client import OllamaClient, OllamaError
from autocoder.llm.prompts import (
    FEATURE_SYSTEM,
    POM_SYSTEM,
    STEPS_SYSTEM,
    build_feature_prompt,
    build_pom_prompt,
    build_steps_prompt,
)
from autocoder.llm.validator import validate_feature_plan, validate_pom_plan
from autocoder.models import (
    FeaturePlan,
    PageExtraction,
    POMPlan,
    ScenarioPlan,
    StepBinding,
    StepRef,
    StepsPlan,
    POMMethod,
)


def _fallback_feature_plan(pom_plan: POMPlan, err: str) -> FeaturePlan:
    """Minimal feature plan used when the LLM cannot produce valid JSON.

    The goal is to keep the rest of the pipeline alive: the POM and
    step bindings still render, and the ``.feature`` file carries a
    single placeholder scenario plus a machine-readable comment the
    user can grep for when auditing a run.
    """
    title = pom_plan.class_name or pom_plan.fixture_name or "page"
    description = (
        f"Auto-fallback feature (LLM JSON failure: {err[:120]}). "
        "Regenerate with `autocoder generate --force` once the LLM is healthy."
    )
    scenario = ScenarioPlan(
        title=f"smoke: render {title}",
        tier="smoke",
        steps=[
            StepRef(
                keyword="Given",
                text=f"I open the {title} page",
                pom_method=None,
            )
        ],
    )
    return FeaturePlan(
        feature=title,
        description=description,
        background=[],
        scenarios=[scenario],
    )


def _read_cache(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.warn("plan_cache_corrupt", path=str(path), err=str(exc))
        return None


def _write_cache(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    logger.debug(
        "plan_cache_write",
        path=str(path),
        action="updated" if existed else "created",
    )


def generate_pom_plan(
    extraction: PageExtraction,
    *,
    page_class: str,
    fixture_name: str,
    client: OllamaClient,
    cache_dir: Path,
    force: bool = False,
) -> POMPlan:
    cache_path = cache_dir / f"{fixture_name}.pom.{extraction.fingerprint}.json"
    cached = None if force else _read_cache(cache_path)
    if cached is not None:
        logger.llm_call(
            model="(cache)",
            purpose=f"pom_plan:{fixture_name}",
            in_tokens=0,
            out_tokens=0,
            duration_s=0.0,
            cached=True,
            cache_path=str(cache_path),
        )
        logger.info(
            "pom_plan_cache_hit",
            fixture=fixture_name,
            fingerprint=extraction.fingerprint,
            path=str(cache_path),
        )
        return POMPlan(**cached)

    if force:
        logger.info("pom_plan_cache_skipped", fixture=fixture_name, reason="--force")
    else:
        logger.info("pom_plan_cache_miss", fixture=fixture_name, fingerprint=extraction.fingerprint)

    user = build_pom_prompt(extraction, page_class=page_class, fixture_name=fixture_name)
    raw = client.chat_json(system=POM_SYSTEM, user=user, purpose=f"pom_plan:{fixture_name}")
    cleaned, issues = validate_pom_plan(raw, extraction.elements)
    if issues:
        logger.warn("pom_plan_validator", fixture=fixture_name, issues=len(issues))
        for msg in issues:
            logger.warn("pom_plan_issue", fixture=fixture_name, msg=msg)
    else:
        logger.ok("pom_plan_validated", fixture=fixture_name, methods=len(cleaned["methods"]))

    plan = POMPlan(
        class_name=cleaned["class_name"] or page_class,
        fixture_name=cleaned["fixture_name"] or fixture_name,
        methods=[POMMethod(**m) for m in cleaned["methods"]],
    )
    _write_cache(cache_path, plan.model_dump(mode="json"))
    return plan


def generate_feature_plan(
    extraction: PageExtraction,
    *,
    pom_plan: POMPlan,
    requested_tiers: list[str],
    client: OllamaClient,
    cache_dir: Path,
    force: bool = False,
    known_pages: list[dict] | None = None,
) -> FeaturePlan:
    method_names = [m.name for m in pom_plan.methods]
    tiers_key = ",".join(sorted(requested_tiers)) or "default"
    cache_path = cache_dir / f"{pom_plan.fixture_name}.feature.{tiers_key}.{extraction.fingerprint}.json"
    cached = None if force else _read_cache(cache_path)
    if cached is not None:
        logger.llm_call(
            model="(cache)",
            purpose=f"feature_plan:{pom_plan.fixture_name}",
            in_tokens=0,
            out_tokens=0,
            duration_s=0.0,
            cached=True,
            cache_path=str(cache_path),
        )
        logger.info(
            "feature_plan_cache_hit",
            fixture=pom_plan.fixture_name,
            tiers=tiers_key,
            fingerprint=extraction.fingerprint,
            path=str(cache_path),
        )
        return FeaturePlan(**cached)

    if force:
        logger.info("feature_plan_cache_skipped", fixture=pom_plan.fixture_name, reason="--force")
    else:
        logger.info(
            "feature_plan_cache_miss",
            fixture=pom_plan.fixture_name,
            tiers=tiers_key,
            fingerprint=extraction.fingerprint,
        )

    user = build_feature_prompt(
        extraction,
        pom_method_names=method_names,
        requested_tiers=requested_tiers,
        known_pages=known_pages,
    )
    try:
        raw = client.chat_json(
            system=FEATURE_SYSTEM,
            user=user,
            purpose=f"feature_plan:{pom_plan.fixture_name}",
        )
    except OllamaError as exc:
        # Do not let a bad JSON response wipe out the POM artifacts.
        # Emit a placeholder feature so steps still render, and leave a
        # cache marker so a future rerun with a healthier LLM will
        # replace it.
        logger.warn(
            "feature_plan_fallback",
            fixture=pom_plan.fixture_name,
            err=str(exc),
            hint="Rerun with --force once the LLM is stable.",
        )
        return _fallback_feature_plan(pom_plan, str(exc))
    cleaned, issues = validate_feature_plan(raw, method_names)
    if issues:
        logger.warn(
            "feature_plan_validator",
            fixture=pom_plan.fixture_name,
            issues=len(issues),
        )
        for msg in issues:
            logger.warn("feature_plan_issue", fixture=pom_plan.fixture_name, msg=msg)
    else:
        logger.ok(
            "feature_plan_validated",
            fixture=pom_plan.fixture_name,
            scenarios=len(cleaned["scenarios"]),
        )

    plan = FeaturePlan(
        feature=cleaned["feature"],
        description=cleaned["description"],
        background=[StepRef(**s) for s in cleaned["background"]],
        scenarios=[
            ScenarioPlan(
                title=s["title"],
                tier=s["tier"],
                steps=[StepRef(**st) for st in s["steps"]],
            )
            for s in cleaned["scenarios"]
        ],
    )
    _write_cache(cache_path, plan.model_dump(mode="json"))
    return plan


# ---------------------------------------------------------------------------
# Prompt 3 — Playwright bodies per Gherkin step
# ---------------------------------------------------------------------------


def _steps_fingerprint(feature_plan: FeaturePlan) -> str:
    """Short hash over every step text in the plan — changes whenever the
    set of Gherkin steps does, so the renderer's LLM output cache stays
    correct across feature-plan edits."""
    h = hashlib.sha256()
    for s in feature_plan.background:
        h.update(s.text.encode("utf-8"))
        h.update(b"\x00")
    for scn in feature_plan.scenarios:
        h.update(scn.title.encode("utf-8"))
        h.update(b"\x00")
        for s in scn.steps:
            h.update(s.text.encode("utf-8"))
            h.update(b"\x00")
    return h.hexdigest()[:12]


def generate_steps_plan(
    extraction: PageExtraction,
    *,
    feature_plan: FeaturePlan,
    pom_plan: POMPlan,
    client: OllamaClient,
    cache_dir: Path,
    force: bool = False,
    known_pages: list[dict] | None = None,
) -> StepsPlan:
    """Call prompt 3 once per page — return a StepsPlan.

    Cache key is ``(fixture, extraction_fingerprint, steps_fingerprint)``
    so reruns on an unchanged feature plan cost zero tokens. The
    renderer ingests :class:`StepsPlan` and uses each binding's body
    verbatim after validation; unbound or invalid bodies fall back to
    the deterministic synthesizer, and ultimately to the
    ``NotImplementedError`` stub path that the heal stage repairs.
    """
    sfp = _steps_fingerprint(feature_plan)
    cache_path = (
        cache_dir
        / f"{pom_plan.fixture_name}.steps.{extraction.fingerprint}.{sfp}.json"
    )
    cached = None if force else _read_cache(cache_path)
    if cached is not None:
        logger.llm_call(
            model="(cache)",
            purpose=f"steps_plan:{pom_plan.fixture_name}",
            in_tokens=0,
            out_tokens=0,
            duration_s=0.0,
            cached=True,
            cache_path=str(cache_path),
        )
        logger.info(
            "steps_plan_cache_hit",
            fixture=pom_plan.fixture_name,
            fingerprint=extraction.fingerprint,
            steps_fp=sfp,
            path=str(cache_path),
        )
        return StepsPlan(**cached)

    if force:
        logger.info("steps_plan_cache_skipped", fixture=pom_plan.fixture_name, reason="--force")
    else:
        logger.info(
            "steps_plan_cache_miss",
            fixture=pom_plan.fixture_name,
            fingerprint=extraction.fingerprint,
            steps_fp=sfp,
        )

    pom_methods_payload = [
        {
            "name": m.name,
            "intent": m.intent,
            "element_id": m.element_id,
            "action": m.action,
            "args": list(m.args or []),
        }
        for m in pom_plan.methods
    ]
    background_payload = [
        {"keyword": s.keyword, "text": s.text, "pom_method": s.pom_method}
        for s in feature_plan.background
    ]
    scenarios_payload = [
        {
            "title": sc.title,
            "tier": sc.tier,
            "steps": [
                {"keyword": s.keyword, "text": s.text, "pom_method": s.pom_method}
                for s in sc.steps
            ],
        }
        for sc in feature_plan.scenarios
    ]

    user = build_steps_prompt(
        extraction,
        feature_plan_scenarios=scenarios_payload,
        feature_plan_background=background_payload,
        pom_class=pom_plan.class_name,
        pom_fixture=pom_plan.fixture_name,
        pom_methods=pom_methods_payload,
        known_pages=known_pages,
    )

    try:
        raw = client.chat_json(
            system=STEPS_SYSTEM,
            user=user,
            purpose=f"steps_plan:{pom_plan.fixture_name}",
        )
    except OllamaError as exc:
        # Non-fatal: the deterministic renderer still produces a file;
        # unbound steps fall through to the NotImplementedError stub
        # path, and the heal stage cleans them up on the first pytest
        # run.
        logger.warn(
            "steps_plan_llm_failed",
            fixture=pom_plan.fixture_name,
            err=str(exc),
            hint="renderer will fall back to deterministic synthesis + heal",
        )
        return StepsPlan(bindings=[])

    bindings_raw = raw.get("bindings")
    if not isinstance(bindings_raw, list):
        logger.warn(
            "steps_plan_no_bindings",
            fixture=pom_plan.fixture_name,
            payload_keys=list(raw.keys()) if isinstance(raw, dict) else [],
        )
        return StepsPlan(bindings=[])

    bindings: list[StepBinding] = []
    seen: set[str] = set()
    for entry in bindings_raw:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("step_text") or "").strip()
        body = str(entry.get("body") or "").strip()
        intent = str(entry.get("intent") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        bindings.append(StepBinding(step_text=text, body=body, intent=intent))

    plan = StepsPlan(bindings=bindings)
    _write_cache(cache_path, plan.model_dump(mode="json"))
    logger.ok(
        "steps_plan_validated",
        fixture=pom_plan.fixture_name,
        bindings=len(bindings),
    )
    return plan
