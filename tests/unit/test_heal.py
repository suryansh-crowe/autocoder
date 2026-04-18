"""Tests for the heal stage (scanner + validator + applier).

LLM calls are not exercised here — the runner is integration-tested
manually via `autocoder heal`. These tests cover the deterministic
parts that decide what gets healed and what gets rejected, which is
where the safety guarantees live.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest

from autocoder.heal.applier import apply_heal
from autocoder.heal.scanner import find_stubs_in_dir, find_stubs_in_file
from autocoder.heal.validator import validate_body


# ---------------------------------------------------------------------------
# Scanner — finds renderer-shaped stubs only
# ---------------------------------------------------------------------------


def _write_steps(tmp_path: Path, body: str) -> Path:
    f = tmp_path / "test_login.py"
    f.write_text(textwrap.dedent(body), encoding="utf-8")
    return f


_HEADER = '''"""Generated step definitions."""
from __future__ import annotations
import pytest
from playwright.sync_api import Page, expect
from pytest_bdd import given, parsers, scenarios, then, when
from tests.pages.login_page import LoginPage

scenarios("login.feature")

@pytest.fixture
def login_page(page: Page) -> LoginPage:
    return LoginPage(page)
'''


def test_scanner_finds_renderer_shaped_stub(tmp_path: Path) -> None:
    f = _write_steps(
        tmp_path,
        _HEADER + '''

@given(parsers.parse('I am on the login page'))
def _i_am_on_the_login_page(login_page: LoginPage) -> None:
    raise NotImplementedError("Implement step: I am on the login page")
''',
    )
    stubs = find_stubs_in_file(f)
    assert len(stubs) == 1
    s = stubs[0]
    assert s.function_name == "_i_am_on_the_login_page"
    assert s.step_text == "I am on the login page"
    assert s.keywords == ("Given",)
    assert s.fixture_name == "login_page"
    assert s.fixture_class == "LoginPage"
    assert s.pom_module == "login_page"
    assert s.slug == "login"


def test_scanner_collects_multiple_decorators(tmp_path: Path) -> None:
    f = _write_steps(
        tmp_path,
        _HEADER + '''

@when(parsers.parse('I click X'))
@given(parsers.parse('I click X'))
def _i_click_x(login_page: LoginPage) -> None:
    raise NotImplementedError("Implement step: I click X")
''',
    )
    stubs = find_stubs_in_file(f)
    assert len(stubs) == 1
    assert set(stubs[0].keywords) == {"When", "Given"}


def test_scanner_skips_hand_edited_body(tmp_path: Path) -> None:
    f = _write_steps(
        tmp_path,
        _HEADER + '''

@given(parsers.parse('I am on the login page'))
def _i_am_on_the_login_page(login_page: LoginPage) -> None:
    login_page.navigate()
''',
    )
    assert find_stubs_in_file(f) == []


def test_scanner_skips_multi_statement_body(tmp_path: Path) -> None:
    f = _write_steps(
        tmp_path,
        _HEADER + '''

@given(parsers.parse('compound'))
def _compound(login_page: LoginPage) -> None:
    print("hi")
    raise NotImplementedError("Implement step: compound")
''',
    )
    # multi-statement body → not the renderer's exact shape, leave alone.
    assert find_stubs_in_file(f) == []


def test_scanner_skips_bodies_with_other_message(tmp_path: Path) -> None:
    f = _write_steps(
        tmp_path,
        _HEADER + '''

@given(parsers.parse('something'))
def _something(login_page: LoginPage) -> None:
    raise NotImplementedError("TODO: something else")
''',
    )
    assert find_stubs_in_file(f) == []


def test_scanner_handles_unparseable_file(tmp_path: Path) -> None:
    f = tmp_path / "test_login.py"
    f.write_text("def broken(:::\n", encoding="utf-8")
    assert find_stubs_in_file(f) == []


def test_scanner_dir_filters_by_slug(tmp_path: Path) -> None:
    a = tmp_path / "test_login.py"
    a.write_text(
        _HEADER + '''
@given(parsers.parse('a'))
def _a(login_page: LoginPage) -> None:
    raise NotImplementedError("Implement step: a")
''',
        encoding="utf-8",
    )
    b = tmp_path / "test_other.py"
    b.write_text(
        _HEADER.replace("login_page", "other_page").replace("LoginPage", "OtherPage")
        + '''
@given(parsers.parse('b'))
def _b(other_page: OtherPage) -> None:
    raise NotImplementedError("Implement step: b")
''',
        encoding="utf-8",
    )
    only_login = find_stubs_in_dir(tmp_path, slug="login")
    assert [s.slug for s in only_login] == ["login"]
    both = find_stubs_in_dir(tmp_path)
    assert sorted(s.slug for s in both) == ["login", "other"]


# ---------------------------------------------------------------------------
# Validator — accept the safe shapes, reject everything else
# ---------------------------------------------------------------------------


_FX = "login_page"
_METHODS = {"navigate", "fill_email", "click_submit"}


def test_validator_accepts_pom_method_call() -> None:
    body, errs = validate_body("login_page.click_submit()", fixture_name=_FX, pom_method_names=_METHODS)
    assert errs == []
    assert body == "login_page.click_submit()"


def test_validator_accepts_navigate_even_if_not_in_methods() -> None:
    body, errs = validate_body("login_page.navigate()", fixture_name=_FX, pom_method_names=set())
    assert errs == []


def test_validator_accepts_locate_chain() -> None:
    body, errs = validate_body(
        "expect(login_page.locate('submit')).to_be_visible()",
        fixture_name=_FX,
        pom_method_names=_METHODS,
    )
    assert errs == []


def test_validator_accepts_pass() -> None:
    body, errs = validate_body("pass", fixture_name=_FX, pom_method_names=_METHODS)
    assert errs == []
    assert body == "pass"


def test_validator_rejects_unknown_method() -> None:
    _, errs = validate_body(
        "login_page.do_thing()", fixture_name=_FX, pom_method_names=_METHODS
    )
    assert any("unknown method" in e for e in errs)


def test_validator_rejects_multi_statement() -> None:
    _, errs = validate_body(
        "login_page.navigate()\nlogin_page.click_submit()",
        fixture_name=_FX,
        pom_method_names=_METHODS,
    )
    assert any("expected exactly one statement" in e for e in errs)


def test_validator_rejects_import() -> None:
    _, errs = validate_body("import os", fixture_name=_FX, pom_method_names=_METHODS)
    assert errs  # parsed as Import — forbidden top-level node


def test_validator_rejects_def() -> None:
    _, errs = validate_body(
        "def evil(): pass", fixture_name=_FX, pom_method_names=_METHODS
    )
    assert errs


def test_validator_rejects_lambda_inside_call() -> None:
    _, errs = validate_body(
        "expect(login_page.page).to_have_url(lambda u: True)",
        fixture_name=_FX,
        pom_method_names=_METHODS,
    )
    assert any("forbidden construct" in e for e in errs)


def test_validator_rejects_syntax_error() -> None:
    _, errs = validate_body("login_page.click_submit(", fixture_name=_FX, pom_method_names=_METHODS)
    assert any("syntax error" in e for e in errs)


def test_validator_rejects_empty_body() -> None:
    _, errs = validate_body("", fixture_name=_FX, pom_method_names=_METHODS)
    assert errs == ["empty body"]


# ---------------------------------------------------------------------------
# Applier — replaces ONLY the stub line and keeps the file parseable
# ---------------------------------------------------------------------------


def test_applier_replaces_stub_line_only(tmp_path: Path) -> None:
    f = _write_steps(
        tmp_path,
        _HEADER + '''

@given(parsers.parse('I am on the login page'))
def _i_am_on_the_login_page(login_page: LoginPage) -> None:
    raise NotImplementedError("Implement step: I am on the login page")


@when(parsers.parse('I click submit'))
def _i_click_submit(login_page: LoginPage) -> None:
    login_page.click_submit()
''',
    )
    stubs = find_stubs_in_file(f)
    assert len(stubs) == 1
    new_text = apply_heal(stubs[0], "login_page.navigate()")
    assert "login_page.navigate()" in new_text
    assert "raise NotImplementedError" not in new_text.split("login_page.click_submit()")[0]
    # Hand-edited sibling untouched.
    assert "login_page.click_submit()" in new_text
    # File still parses.
    ast.parse(new_text)


def test_applier_preserves_indentation(tmp_path: Path) -> None:
    f = _write_steps(
        tmp_path,
        _HEADER + '''

@given(parsers.parse('a'))
def _a(login_page: LoginPage) -> None:
    raise NotImplementedError("Implement step: a")
''',
    )
    stubs = find_stubs_in_file(f)
    new_text = apply_heal(stubs[0], "login_page.navigate()")
    assert "    login_page.navigate()" in new_text  # 4-space indent kept


def test_applier_aborts_when_result_does_not_parse(tmp_path: Path) -> None:
    f = _write_steps(
        tmp_path,
        _HEADER + '''

@given(parsers.parse('a'))
def _a(login_page: LoginPage) -> None:
    raise NotImplementedError("Implement step: a")
''',
    )
    stubs = find_stubs_in_file(f)
    with pytest.raises(ValueError, match="broke parse"):
        apply_heal(stubs[0], "login_page.navigate(")


def test_applier_idempotent_when_run_via_real_validation(tmp_path: Path) -> None:
    """Body that's already a valid heal target (not a stub) is not
    re-found by the scanner, so a second pass finds nothing."""
    f = _write_steps(
        tmp_path,
        _HEADER + '''

@given(parsers.parse('a'))
def _a(login_page: LoginPage) -> None:
    raise NotImplementedError("Implement step: a")
''',
    )
    stubs = find_stubs_in_file(f)
    new_text = apply_heal(stubs[0], "login_page.navigate()")
    f.write_text(new_text, encoding="utf-8")
    assert find_stubs_in_file(f) == []
