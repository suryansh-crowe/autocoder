"""Render Gherkin feature files from a validated FeaturePlan."""

from __future__ import annotations

from autocoder.models import FeaturePlan, ScenarioPlan, StepRef


_TIER_TAG = {
    "smoke": "@smoke",
    "sanity": "@sanity",
    "regression": "@regression",
    "happy": "@smoke",
    "edge": "@regression @edge",
    "validation": "@regression @validation",
    "navigation": "@regression @navigation",
    "auth": "@auth",
    "rbac": "@regression @rbac",
    "e2e": "@e2e",
}


def _step_line(step: StepRef) -> str:
    return f"    {step.keyword} {step.text}"


def _scenario_block(scn: ScenarioPlan) -> str:
    tag_line = _TIER_TAG.get(scn.tier, "@regression")
    lines = [f"  {tag_line}", f"  Scenario: {scn.title}"]
    lines.extend(_step_line(s) for s in scn.steps)
    return "\n".join(lines)


def render_feature(plan: FeaturePlan) -> str:
    lines: list[str] = []
    lines.append(f"Feature: {plan.feature}")
    if plan.description:
        for desc_line in plan.description.splitlines():
            lines.append(f"  {desc_line}")
    lines.append("")
    if plan.background:
        lines.append("  Background:")
        for s in plan.background:
            lines.append(_step_line(s))
        lines.append("")
    for scn in plan.scenarios:
        lines.append(_scenario_block(scn))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
