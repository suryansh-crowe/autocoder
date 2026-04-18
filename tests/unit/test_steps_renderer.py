"""Tests for autocoder.generate.steps — the step-definition renderer.

Two important guarantees the renderer must keep:

1. The same step text under multiple Gherkin keywords (e.g. "When"
   in one scenario and "Given" in another) collapses to ONE Python
   function with stacked decorators — never two functions sharing
   a name.
2. POM methods that require value arguments must not be called
   without them. When the step text supplies no quoted value and
   the method needs one, the body raises ``NotImplementedError``.
"""

from __future__ import annotations

import ast

import pytest

from autocoder.generate.steps import render_steps
from autocoder.models import FeaturePlan, POMMethod, POMPlan, ScenarioPlan, StepRef


def _plan(
    *,
    methods: list[POMMethod] | None = None,
    background: list[StepRef] | None = None,
    scenarios: list[ScenarioPlan] | None = None,
) -> tuple[POMPlan, FeaturePlan]:
    pom = POMPlan(
        class_name="LoginPage",
        fixture_name="login_page",
        methods=methods or [],
    )
    feat = FeaturePlan(
        feature="Login",
        description="",
        background=background or [],
        scenarios=scenarios or [],
    )
    return pom, feat


def _render(pom: POMPlan, feat: FeaturePlan) -> str:
    return render_steps(
        feature_title=feat.feature,
        feature_path="login.feature",
        feature_plan=feat,
        pom_plan=pom,
        pom_module="login_page",
    )


def _function_names(source: str) -> list[str]:
    tree = ast.parse(source)
    return [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]


def test_renderer_emits_valid_python() -> None:
    pom, feat = _plan(
        scenarios=[
            ScenarioPlan(
                title="ok",
                tier="smoke",
                steps=[StepRef(keyword="Given", text="I am on login")],
            )
        ]
    )
    src = _render(pom, feat)
    ast.parse(src)  # raises if invalid


def test_same_text_under_two_keywords_emits_one_function() -> None:
    pom, feat = _plan(
        methods=[POMMethod(name="click_terms", intent="", element_id="terms", action="click")],
        scenarios=[
            ScenarioPlan(
                title="A",
                tier="smoke",
                steps=[
                    StepRef(keyword="When", text="I click terms", pom_method="click_terms"),
                ],
            ),
            ScenarioPlan(
                title="B",
                tier="happy",
                steps=[
                    StepRef(keyword="Given", text="I click terms", pom_method="click_terms"),
                ],
            ),
        ],
    )
    src = _render(pom, feat)
    names = _function_names(src)
    # Exactly one Python function for the shared text (plus the fixture).
    step_funcs = [n for n in names if n.startswith("_")]
    assert step_funcs == ["_i_click_terms"], step_funcs
    # Both decorators present.
    assert "@when(parsers.parse(" in src
    assert "@given(parsers.parse(" in src


def test_and_inherits_previous_keyword() -> None:
    pom, feat = _plan(
        methods=[POMMethod(name="click_a", intent="", element_id="a", action="click")],
        scenarios=[
            ScenarioPlan(
                title="A",
                tier="smoke",
                steps=[
                    StepRef(keyword="When", text="I click a", pom_method="click_a"),
                    StepRef(keyword="And", text="I click b"),
                ],
            )
        ],
    )
    src = _render(pom, feat)
    # The 'And' step inherited 'When' → @when decorator.
    when_lines = [line for line in src.splitlines() if line.startswith("@when(")]
    # Two @when decorators (one per unique step text).
    assert len(when_lines) == 2, when_lines
    # No @given for steps from this scenario.
    assert "@given(" not in src


def test_fill_method_without_arg_raises_notimplemented() -> None:
    pom, feat = _plan(
        methods=[
            POMMethod(
                name="fill_email",
                intent="",
                element_id="email",
                action="fill",
                args=["value"],
            )
        ],
        scenarios=[
            ScenarioPlan(
                title="X",
                tier="smoke",
                steps=[
                    StepRef(
                        keyword="When",
                        text="I fill in my email",
                        pom_method="fill_email",
                    ),
                ],
            )
        ],
    )
    src = _render(pom, feat)
    assert "login_page.fill_email()" not in src
    assert 'raise NotImplementedError(' in src
    assert "expects: value" in src


def test_fill_method_with_quoted_value_calls_normally() -> None:
    pom, feat = _plan(
        methods=[
            POMMethod(
                name="fill_email",
                intent="",
                element_id="email",
                action="fill",
                args=["value"],
            )
        ],
        scenarios=[
            ScenarioPlan(
                title="X",
                tier="smoke",
                steps=[
                    StepRef(
                        keyword="When",
                        text='I fill in my email "x@y.com"',
                        pom_method="fill_email",
                    ),
                ],
            )
        ],
    )
    src = _render(pom, feat)
    assert "login_page.fill_email(arg0)" in src
    # The captured arg name must appear in the function signature too.
    assert "arg0: str" in src


def test_step_without_pom_method_raises_notimplemented() -> None:
    pom, feat = _plan(
        scenarios=[
            ScenarioPlan(
                title="X",
                tier="smoke",
                steps=[StepRef(keyword="Then", text="I should be on the home page")],
            )
        ],
    )
    src = _render(pom, feat)
    assert 'raise NotImplementedError("Implement step: I should be on the home page")' in src


def test_no_duplicate_function_names_across_whole_render() -> None:
    pom, feat = _plan(
        methods=[POMMethod(name="click_x", intent="", element_id="x", action="click")],
        background=[StepRef(keyword="Given", text="I am on the login page")],
        scenarios=[
            ScenarioPlan(
                title="A",
                tier="smoke",
                steps=[
                    StepRef(keyword="When", text="I click x", pom_method="click_x"),
                    StepRef(keyword="Then", text="I am on the login page"),
                ],
            ),
            ScenarioPlan(
                title="B",
                tier="happy",
                steps=[
                    StepRef(keyword="Given", text="I click x", pom_method="click_x"),
                ],
            ),
        ],
    )
    src = _render(pom, feat)
    names = _function_names(src)
    assert len(names) == len(set(names)), f"duplicates: {names}"
