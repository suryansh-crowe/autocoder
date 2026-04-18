"""Grammar-validate JSON plans against the extracted catalog.

The validator runs *before* templates render. It rejects:

* unknown element ids (model hallucinated)
* duplicate method names
* invalid action verbs
* unknown POM methods inside a feature plan

The orchestrator can decide what to do with the resulting issues —
typically: drop the offending entry, log a warning, and keep the rest.
"""

from __future__ import annotations

from typing import Any

from autocoder.models import Element


_VALID_ACTIONS = {
    "click",
    "fill",
    "check",
    "select",
    "navigate",
    "wait",
    "expect_visible",
    "expect_text",
}
_FILL_LIKE = {"fill", "select"}


def validate_pom_plan(plan: dict[str, Any], elements: list[Element]) -> tuple[dict[str, Any], list[str]]:
    """Return (cleaned_plan, issues)."""
    issues: list[str] = []
    by_id = {e.id: e for e in elements}
    seen_names: set[str] = set()

    methods_in = plan.get("methods") or []
    methods_out: list[dict[str, Any]] = []

    for raw in methods_in:
        if not isinstance(raw, dict):
            issues.append(f"method entry not an object: {raw!r}")
            continue
        name = (raw.get("name") or "").strip()
        eid = (raw.get("element_id") or "").strip()
        action = (raw.get("action") or "").strip()
        intent = (raw.get("intent") or "").strip()
        args = raw.get("args") or []

        if not name:
            issues.append("method missing name")
            continue
        if name in seen_names:
            issues.append(f"duplicate method name dropped: {name}")
            continue
        if action not in _VALID_ACTIONS:
            issues.append(f"unknown action {action!r} on {name!r}")
            continue
        if action != "navigate" and eid not in by_id:
            issues.append(f"unknown element_id {eid!r} on {name!r}")
            continue
        if action in _FILL_LIKE and not args:
            args = ["value"]
        if not isinstance(args, list):
            issues.append(f"args not a list on {name!r}")
            continue

        seen_names.add(name)
        methods_out.append(
            {
                "name": name,
                "intent": intent or name.replace("_", " "),
                "element_id": eid,
                "action": action,
                "args": [str(a) for a in args],
            }
        )

    cleaned = {
        "class_name": (plan.get("class_name") or "").strip() or "GeneratedPage",
        "fixture_name": (plan.get("fixture_name") or "").strip() or "generated_page",
        "methods": methods_out,
    }
    return cleaned, issues


def validate_feature_plan(
    plan: dict[str, Any],
    pom_method_names: list[str],
) -> tuple[dict[str, Any], list[str]]:
    issues: list[str] = []
    valid_methods = set(pom_method_names)

    def _clean_step(raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            issues.append(f"step entry not an object: {raw!r}")
            return None
        kw = (raw.get("keyword") or "").strip().capitalize()
        if kw not in {"Given", "When", "Then", "And"}:
            issues.append(f"invalid step keyword {kw!r}")
            return None
        text = (raw.get("text") or "").strip()
        if not text:
            issues.append("step missing text")
            return None
        method = raw.get("pom_method")
        if method and method not in valid_methods:
            issues.append(f"unknown pom_method {method!r} -> falling back to manual step")
            method = None
        args = raw.get("args") or []
        if not isinstance(args, list):
            args = []
        return {"keyword": kw, "text": text, "pom_method": method, "args": [str(a) for a in args]}

    background_in = plan.get("background") or []
    background_out: list[dict[str, Any]] = []
    for raw in background_in:
        cleaned = _clean_step(raw)
        if cleaned is not None:
            background_out.append(cleaned)

    scenarios_in = plan.get("scenarios") or []
    scenarios_out: list[dict[str, Any]] = []
    seen_titles: set[str] = set()
    for raw_scn in scenarios_in:
        if not isinstance(raw_scn, dict):
            issues.append("scenario entry not an object")
            continue
        title = (raw_scn.get("title") or "").strip()
        if not title:
            issues.append("scenario missing title")
            continue
        if title in seen_titles:
            issues.append(f"duplicate scenario title dropped: {title}")
            continue
        tier = (raw_scn.get("tier") or "smoke").strip().lower()
        steps_clean = [s for s in (_clean_step(r) for r in raw_scn.get("steps") or []) if s]
        if not steps_clean:
            issues.append(f"scenario {title!r} has no usable steps")
            continue
        seen_titles.add(title)
        scenarios_out.append({"title": title, "tier": tier, "steps": steps_clean})

    if not scenarios_out:
        issues.append("plan produced no usable scenarios")

    cleaned = {
        "feature": (plan.get("feature") or "Generated feature").strip(),
        "description": (plan.get("description") or "").strip(),
        "background": background_out,
        "scenarios": scenarios_out,
    }
    return cleaned, issues
