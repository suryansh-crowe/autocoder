"""Tests for the failure-driven heal pipeline.

LLM calls aren't exercised here. We verify:

* JUnit XML parsing extracts test_id, file path, error message,
  step function (deepest `_<name>` in the trace), and the failure
  classifier label.
* The multi-statement validator accepts safe sequences and rejects
  the same dangerous shapes as the single-statement validator.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from autocoder.heal.pytest_failures import (
    PytestFailure,
    classify,
    parse_junit_xml,
)
from autocoder.heal.validator import validate_body


# ---------------------------------------------------------------------------
# classify() — failure-bucket heuristics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msg, expected",
    [
        ('Locator.click: Timeout 30000ms exceeded.\n  - element is not enabled', "disabled"),
        ('Locator.click: Timeout 30000ms exceeded.\n  - <div ...> intercepts pointer events', "intercepted"),
        ('Locator.fill: Error: Input of type "checkbox" cannot be filled', "wrong_kind"),
        ('LocatorNotFound: no selector resolved', "locator_not_found"),
        ('element is not visible', "not_visible"),
        ('element is not attached', "not_attached"),
        ('Locator.click: Timeout 30000ms exceeded', "timeout"),
        ('AssertionError: True != False', "other"),
    ],
)
def test_classify_buckets(msg: str, expected: str) -> None:
    assert classify(msg) == expected


# ---------------------------------------------------------------------------
# parse_junit_xml() — extract structured failures
# ---------------------------------------------------------------------------


_JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
<testsuite name="pytest" failures="2" tests="2">
<testcase classname="tests.steps.test_login" name="test_smoke_login" file="tests/steps/test_login.py">
<failure message="Locator.click: Timeout 30000ms exceeded." type="playwright._impl._errors.TimeoutError">
tests/steps/test_login.py:28: in _i_click_sign_in_with_microsoft
    login_page.click_sign_in_with_microsoft()
tests/pages/login_page.py:34: in click_sign_in_with_microsoft
    self.locate('sign_in_with_microsoft').click()
playwright._impl._errors.TimeoutError: Locator.click: Timeout 30000ms exceeded.
  - element is not enabled
</failure>
</testcase>
<testcase classname="tests.steps.test_login" name="test_validation_email" file="tests/steps/test_login.py">
<failure message='Input of type "checkbox" cannot be filled' type="playwright._impl._errors.Error">
tests/steps/test_login.py:48: in _i_fill_in_my_email_address
    login_page.fill_email('your-email@example.com')
playwright._impl._errors.Error: Locator.fill: Error: Input of type "checkbox" cannot be filled
</failure>
</testcase>
</testsuite>
</testsuites>
"""


def test_parse_junit_xml_extracts_failures(tmp_path: Path) -> None:
    p = tmp_path / "report.xml"
    p.write_text(_JUNIT, encoding="utf-8")
    fails = parse_junit_xml(p, base=tmp_path)

    assert len(fails) == 2
    f1, f2 = fails

    assert f1.test_id.endswith("::test_smoke_login")
    assert f1.step_function == "_i_click_sign_in_with_microsoft"
    assert f1.failure_class == "disabled"
    assert "Timeout" in f1.error_message

    assert f2.step_function == "_i_fill_in_my_email_address"
    assert f2.failure_class == "wrong_kind"


def test_parse_junit_xml_skips_passing_tests(tmp_path: Path) -> None:
    p = tmp_path / "report.xml"
    p.write_text(
        '<?xml version="1.0"?><testsuites><testsuite>'
        '<testcase classname="tests.steps.test_x" name="test_pass"/>'
        '</testsuite></testsuites>',
        encoding="utf-8",
    )
    assert parse_junit_xml(p, base=tmp_path) == []


def test_parse_junit_xml_handles_error_elements(tmp_path: Path) -> None:
    """Pytest emits `<error>` for collection errors; treat them like failures."""
    p = tmp_path / "report.xml"
    p.write_text(
        '<?xml version="1.0"?><testsuites><testsuite><testcase '
        'classname="tests.steps.test_login" name="test_x">'
        '<error message="boom" type="ImportError">tests/steps/test_login.py:1: ImportError</error>'
        '</testcase></testsuite></testsuites>',
        encoding="utf-8",
    )
    fails = parse_junit_xml(p, base=tmp_path)
    assert len(fails) == 1
    assert fails[0].error_type == "ImportError"


def test_parse_junit_xml_takes_deepest_step_function(tmp_path: Path) -> None:
    """When the trace touches multiple `_<name>` callers, pick the deepest."""
    p = tmp_path / "report.xml"
    p.write_text(
        '<?xml version="1.0"?><testsuites><testsuite><testcase '
        'classname="tests.steps.test_login" name="t">'
        '<failure type="X" message="boom">'
        'tests/steps/test_login.py:10: in _i_setup\n  call()\n'
        'tests/steps/test_login.py:20: in _i_actual_failure\n  fail()\n'
        '</failure>'
        '</testcase></testsuite></testsuites>',
        encoding="utf-8",
    )
    fails = parse_junit_xml(p, base=tmp_path)
    assert fails[0].step_function == "_i_actual_failure"


# ---------------------------------------------------------------------------
# Multi-statement validator
# ---------------------------------------------------------------------------


_FX = "login_page"
_METHODS = {"click_sign_in_with_microsoft", "fill_email", "navigate"}


def test_multi_statement_accepts_prerequisite_then_action() -> None:
    body = "login_page.locate('agreement').check()\nlogin_page.click_sign_in_with_microsoft()"
    cleaned, errs = validate_body(
        body, fixture_name=_FX, pom_method_names=_METHODS, max_statements=5
    )
    assert errs == []
    assert "check()" in cleaned and "click_sign_in_with_microsoft" in cleaned


def test_multi_statement_rejects_too_many_statements() -> None:
    body = "\n".join(["login_page.navigate()"] * 6)
    _, errs = validate_body(body, fixture_name=_FX, pom_method_names=_METHODS, max_statements=5)
    assert any("too many statements" in e for e in errs)


def test_multi_statement_rejects_unknown_method_in_any_statement() -> None:
    body = "login_page.navigate()\nlogin_page.fake_action()"
    _, errs = validate_body(body, fixture_name=_FX, pom_method_names=_METHODS, max_statements=5)
    assert any("unknown method" in e and "fake_action" in e for e in errs)


def test_multi_statement_default_still_one() -> None:
    """Default max_statements stays at 1 so stub heal can't accidentally
    accept multi-statement bodies — only the failure path opts in."""
    body = "login_page.navigate()\nlogin_page.click_sign_in_with_microsoft()"
    _, errs = validate_body(body, fixture_name=_FX, pom_method_names=_METHODS)
    assert any("too many" in e for e in errs)
