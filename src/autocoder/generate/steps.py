"""Render pytest-bdd step definition files.

Steps are emitted *deterministically* from the FeaturePlan + POMPlan:

* Each unique step **text** becomes one Python function.
* That function is decorated with every Gherkin keyword the text was
  used under across the feature (Given/When/Then). pytest-bdd allows
  stacking decorators on a single function, so we never end up with
  two functions sharing a Python name.
* If the step references a POM method, the body calls
  ``page_object.<method>(*args)`` — and only when the step actually
  supplies values for every parameter the POM method requires. When
  args are missing, the body raises ``NotImplementedError`` so the
  failing step is loud, not silently broken.
* If the step does not reference a POM method at all, the body
  raises ``NotImplementedError("Implement step: ...")``.
"""

from __future__ import annotations

import re
from collections import OrderedDict

from autocoder.models import FeaturePlan, POMPlan, StepRef


_HEADER = '''"""Generated step definitions for {feature_title!r}."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect
from pytest_bdd import given, parsers, scenarios, then, when

from tests.pages.{module_name} import {class_name}

scenarios("{feature_path}")


@pytest.fixture
def {fixture_name}(page: Page) -> {class_name}:
    return {class_name}(page)
'''


_STEP_TPL = '''

{decorators}
def _{slug}({fixture_name}: {class_name}{extra_params}) -> None:
    {body}
'''


# Map Gherkin keywords to pytest-bdd decorators.
# `And` / `But` inherit the previous step's keyword in Gherkin; we
# resolve them at parse time, so by the time they reach _decorator_for
# they should already have been promoted. As a fallback, treat them
# as @given (the most permissive matcher).
_KEYWORD_TO_DECORATOR = {
    "Given": "given",
    "When": "when",
    "Then": "then",
    "And": "given",
    "But": "given",
}


def _slug(text: str, idx: int) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    if not cleaned:
        cleaned = f"step_{idx}"
    return cleaned[:50]


def _matcher(text: str) -> tuple[str, list[str]]:
    """Return a parsers.parse-style matcher and any extracted arg names.

    Quoted segments become {arg0}, {arg1}, ...
    """
    parts: list[str] = []
    args: list[str] = []
    last = 0
    for i, m in enumerate(re.finditer(r'"([^"]+)"', text)):
        parts.append(text[last : m.start()])
        arg = f"arg{i}"
        parts.append(f'"{{{arg}}}"')
        args.append(arg)
        last = m.end()
    parts.append(text[last:])
    pattern = "".join(parts)
    return f"parsers.parse({pattern!r})", args


def _resolve_keywords(steps: list[StepRef]) -> list[StepRef]:
    """Promote ``And`` / ``But`` to the keyword of the previous step.

    Operates in scenario / background order so the promotion is correct
    even when a scenario opens with ``And`` (defensive fallback to
    ``Given`` in that case).
    """
    out: list[StepRef] = []
    last_kw = "Given"
    for s in steps:
        kw = s.keyword
        if kw in {"And", "But"}:
            kw = last_kw
        else:
            last_kw = kw
        if kw == s.keyword:
            out.append(s)
        else:
            out.append(s.model_copy(update={"keyword": kw}))
    return out


def _body(
    step: StepRef,
    extracted_args: list[str],
    fixture_name: str,
    pom_args_by_method: dict[str, list[str]],
) -> str:
    if not step.pom_method:
        return f'raise NotImplementedError("Implement step: {step.text}")'

    required = pom_args_by_method.get(step.pom_method, [])
    supplied = list(extracted_args) + list(step.args or [])
    if len(supplied) < len(required):
        missing = ", ".join(required[len(supplied):])
        return (
            f'raise NotImplementedError('
            f'"Implement step: {step.text} '
            f'(POM method {step.pom_method!r} expects: {missing})")'
        )

    return f"{fixture_name}.{step.pom_method}({', '.join(supplied[: len(required)] or supplied)})"


def render_steps(
    *,
    feature_title: str,
    feature_path: str,
    feature_plan: FeaturePlan,
    pom_plan: POMPlan,
    pom_module: str,
) -> str:
    """Emit a step-definitions module covering every step in the plan."""
    fixture_name = pom_plan.fixture_name
    class_name = pom_plan.class_name
    pom_args_by_method = {m.name: list(m.args or []) for m in pom_plan.methods}

    parts: list[str] = [
        _HEADER.format(
            feature_title=feature_title,
            module_name=pom_module,
            class_name=class_name,
            feature_path=feature_path,
            fixture_name=fixture_name,
        )
    ]

    # Background runs once before each scenario in Gherkin terms; the
    # keyword inheritance still applies inside background.
    background_resolved = _resolve_keywords(list(feature_plan.background))
    all_steps: list[StepRef] = list(background_resolved)
    for scn in feature_plan.scenarios:
        all_steps.extend(_resolve_keywords(list(scn.steps)))

    # Group by step text. Same text under multiple keywords becomes a
    # single function decorated with each keyword's decorator.
    by_text: "OrderedDict[str, dict]" = OrderedDict()
    for step in all_steps:
        entry = by_text.get(step.text)
        if entry is None:
            entry = {"step": step, "keywords": []}
            by_text[step.text] = entry
        kw = step.keyword
        if kw not in entry["keywords"]:
            entry["keywords"].append(kw)

    for idx, (text, entry) in enumerate(by_text.items()):
        step: StepRef = entry["step"]
        keywords: list[str] = entry["keywords"]

        matcher, extracted_args = _matcher(text)
        decorators = "\n".join(
            f"@{_KEYWORD_TO_DECORATOR.get(kw, 'given')}({matcher})"
            for kw in keywords
        )
        slug = _slug(text, idx)
        body = _body(step, extracted_args, fixture_name, pom_args_by_method)
        extra_params = "".join(f", {a}: str" for a in extracted_args)

        parts.append(
            _STEP_TPL.format(
                decorators=decorators,
                slug=slug,
                fixture_name=fixture_name,
                class_name=class_name,
                extra_params=extra_params,
                body=body,
            )
        )

    return "".join(parts)
