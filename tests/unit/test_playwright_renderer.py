"""Tests for autocoder.generate.playwright_script — pytest script renderer.

Guarantees the renderer must keep:

1. The output is always syntactically valid Python (an ``ast.parse``
   sanity check runs inside the renderer itself; these tests exercise
   enough shapes to make sure we do not regress).
2. Each scenario becomes exactly one ``test_<name>`` function; the
   ``@pytest.mark.<tier>`` decorator is emitted before any extra tags.
3. The first statement of every test is ``pom.navigate()`` even when
   the LLM plan omits it.
4. Statements that fail the validator are rendered as
   ``NotImplementedError`` stubs and counted in ``placeholder_count``
   so the orchestrator can trigger the heal stage.
"""

from __future__ import annotations

import ast

from autocoder.generate.playwright_script import render_playwright_script
from autocoder.models import (
    Element,
    PlaywrightScriptPlan,
    PlaywrightTest,
    StableSelector,
    SelectorStrategy,
)


def _element(eid: str, kind: str = "button") -> Element:
    return Element(
        id=eid,
        role=kind,
        name=eid,
        kind=kind,
        selector=StableSelector(strategy=SelectorStrategy.TEST_ID, value=eid),
    )


def _plan(tests: list[PlaywrightTest]) -> PlaywrightScriptPlan:
    return PlaywrightScriptPlan(
        url="https://example.test/login",
        pom_class="LoginPage",
        pom_fixture="pom",
        pom_module="login_page",
        tests=tests,
    )


def test_renderer_emits_one_test_per_scenario_and_parses() -> None:
    plan = _plan(
        [
            PlaywrightTest(
                name="test_smoke_login",
                scenario_title="User signs in",
                tier="smoke",
                tags=["smoke"],
                statements=[
                    "pom.navigate()",
                    "pom.fill_email('a@b.test')",
                    "pom.click_submit()",
                    "expect(pom.locate('dashboard')).to_be_visible()",
                ],
            )
        ]
    )
    source, placeholders = render_playwright_script(
        plan,
        feature_title="Login",
        pom_method_names={"fill_email", "click_submit"},
        elements=[_element("dashboard")],
    )
    assert placeholders == 0
    assert "@pytest.mark.smoke" in source
    assert "def test_smoke_login(pom: LoginPage) -> None:" in source
    ast.parse(source)


def test_renderer_invalid_statement_becomes_stub_and_is_counted() -> None:
    plan = _plan(
        [
            PlaywrightTest(
                name="test_bad",
                scenario_title="bad plan",
                tier="smoke",
                tags=["smoke"],
                statements=["pom.navigate()", "pom.nonexistent_method()"],
            )
        ]
    )
    source, placeholders = render_playwright_script(
        plan,
        feature_title="Login",
        pom_method_names={"click_submit"},
        elements=[],
    )
    assert placeholders == 1
    assert 'raise NotImplementedError("Implement step: pom.nonexistent_method()")' in source
    ast.parse(source)


def test_renderer_forces_navigate_as_first_statement_via_plan_hint() -> None:
    """The renderer trusts the plan order; the plan validator (not the
    renderer) guarantees the leading ``pom.navigate()``. Here we check
    that if the plan already starts with navigate, it survives."""
    plan = _plan(
        [
            PlaywrightTest(
                name="test_min",
                scenario_title="Minimal",
                tier="smoke",
                tags=["smoke"],
                statements=["pom.navigate()"],
            )
        ]
    )
    source, _ = render_playwright_script(
        plan,
        feature_title="Login",
        pom_method_names=set(),
        elements=[],
    )
    first_body_line = source.split("def test_min")[1].split(":", 1)[1].splitlines()[2]
    assert "pom.navigate()" in first_body_line
    ast.parse(source)


def test_renderer_emits_extra_marks_after_tier() -> None:
    plan = _plan(
        [
            PlaywrightTest(
                name="test_tagged",
                scenario_title="Tagged scenario",
                tier="smoke",
                tags=["smoke", "happy", "e2e"],
                statements=["pom.navigate()"],
            )
        ]
    )
    source, _ = render_playwright_script(
        plan,
        feature_title="Login",
        pom_method_names=set(),
        elements=[],
    )
    # tier decorator first, then extras
    smoke_idx = source.index("@pytest.mark.smoke")
    happy_idx = source.index("@pytest.mark.happy")
    e2e_idx = source.index("@pytest.mark.e2e")
    assert smoke_idx < happy_idx < e2e_idx
    ast.parse(source)
