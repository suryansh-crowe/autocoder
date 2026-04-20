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


def _write_tests(tmp_path: Path, body: str, *, filename: str = "test_login.py") -> Path:
    f = tmp_path / filename
    f.write_text(textwrap.dedent(body), encoding="utf-8")
    return f


_HEADER = '''"""Generated Playwright tests for Login."""
from __future__ import annotations
import pytest
from playwright.sync_api import Page, expect
from tests.pages.login_page import LoginPage


@pytest.fixture
def pom(page: Page) -> LoginPage:
    return LoginPage(page)
'''


def test_scanner_finds_renderer_shaped_stub(tmp_path: Path) -> None:
    f = _write_tests(
        tmp_path,
        _HEADER + '''

@pytest.mark.smoke
def test_login(pom: LoginPage) -> None:
    """User signs in"""
    raise NotImplementedError("Implement step: sign in")
''',
    )
    stubs = find_stubs_in_file(f)
    assert len(stubs) == 1
    s = stubs[0]
    assert s.function_name == "test_login"
    assert s.scenario_title == "User signs in"
    assert s.fixture_name == "pom"
    assert s.fixture_class == "LoginPage"
    assert s.pom_module == "login_page"
    assert s.slug == "login"


def test_scanner_skips_hand_edited_body(tmp_path: Path) -> None:
    f = _write_tests(
        tmp_path,
        _HEADER + '''

@pytest.mark.smoke
def test_login(pom: LoginPage) -> None:
    """hand edited"""
    pom.navigate()
''',
    )
    assert find_stubs_in_file(f) == []


def test_scanner_skips_multi_statement_body(tmp_path: Path) -> None:
    f = _write_tests(
        tmp_path,
        _HEADER + '''

def test_compound(pom: LoginPage) -> None:
    """compound"""
    pom.navigate()
    raise NotImplementedError("Implement step: compound")
''',
    )
    # Multi-statement body → not the renderer's exact shape, leave alone.
    assert find_stubs_in_file(f) == []


def test_scanner_accepts_docstring_plus_stub(tmp_path: Path) -> None:
    """A function whose body is (docstring, raise NotImplementedError) IS a stub."""
    f = _write_tests(
        tmp_path,
        _HEADER + '''

def test_with_doc(pom: LoginPage) -> None:
    """A scenario title"""
    raise NotImplementedError("Implement step: thing")
''',
    )
    stubs = find_stubs_in_file(f)
    assert len(stubs) == 1
    assert stubs[0].scenario_title == "A scenario title"


def test_scanner_skips_bodies_with_other_message(tmp_path: Path) -> None:
    f = _write_tests(
        tmp_path,
        _HEADER + '''

def test_something(pom: LoginPage) -> None:
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

def test_a(pom: LoginPage) -> None:
    raise NotImplementedError("Implement step: a")
''',
        encoding="utf-8",
    )
    b = tmp_path / "test_other.py"
    b.write_text(
        _HEADER.replace("login_page", "other_page").replace("LoginPage", "OtherPage")
        + '''

def test_b(pom: OtherPage) -> None:
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


_FX = "pom"
_METHODS = {"navigate", "fill_email", "click_submit"}


def test_validator_accepts_pom_method_call() -> None:
    body, errs = validate_body("pom.click_submit()", fixture_name=_FX, pom_method_names=_METHODS)
    assert errs == []
    assert body == "pom.click_submit()"


def test_validator_accepts_navigate_even_if_not_in_methods() -> None:
    body, errs = validate_body("pom.navigate()", fixture_name=_FX, pom_method_names=set())
    assert errs == []


def test_validator_accepts_locate_chain() -> None:
    body, errs = validate_body(
        "expect(pom.locate('submit')).to_be_visible()",
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
        "pom.do_thing()", fixture_name=_FX, pom_method_names=_METHODS
    )
    assert any("unknown method" in e for e in errs)


def test_validator_rejects_multi_statement() -> None:
    _, errs = validate_body(
        "pom.navigate()\npom.click_submit()",
        fixture_name=_FX,
        pom_method_names=_METHODS,
    )
    # Default max_statements=1; the failure mode opts in to higher caps.
    assert any("too many statements" in e for e in errs)


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
        "expect(pom.page).to_have_url(lambda u: True)",
        fixture_name=_FX,
        pom_method_names=_METHODS,
    )
    assert any("forbidden construct" in e for e in errs)


def test_validator_rejects_syntax_error() -> None:
    _, errs = validate_body("pom.click_submit(", fixture_name=_FX, pom_method_names=_METHODS)
    assert any("syntax error" in e for e in errs)


def test_validator_rejects_empty_body() -> None:
    _, errs = validate_body("", fixture_name=_FX, pom_method_names=_METHODS)
    assert errs == ["empty body"]


# ---------------------------------------------------------------------------
# Applier — replaces the whole test body (docstring untouched) and keeps
# the file parseable.
# ---------------------------------------------------------------------------


def test_applier_replaces_body_only(tmp_path: Path) -> None:
    f = _write_tests(
        tmp_path,
        _HEADER + '''

def test_login(pom: LoginPage) -> None:
    """Sign in"""
    raise NotImplementedError("Implement step: sign in")


def test_submit(pom: LoginPage) -> None:
    """Submit"""
    pom.click_submit()
''',
    )
    stubs = find_stubs_in_file(f)
    assert len(stubs) == 1
    new_text = apply_heal(stubs[0], "pom.navigate()\npom.click_submit()")
    assert "pom.navigate()" in new_text
    assert "raise NotImplementedError" not in new_text.split("def test_submit")[0]
    # Hand-edited sibling untouched.
    assert "def test_submit" in new_text and "pom.click_submit()" in new_text
    # File still parses.
    ast.parse(new_text)


def test_applier_preserves_indentation(tmp_path: Path) -> None:
    f = _write_tests(
        tmp_path,
        _HEADER + '''

def test_a(pom: LoginPage) -> None:
    raise NotImplementedError("Implement step: a")
''',
    )
    stubs = find_stubs_in_file(f)
    new_text = apply_heal(stubs[0], "pom.navigate()")
    assert "    pom.navigate()" in new_text  # 4-space indent kept


def test_applier_aborts_when_result_does_not_parse(tmp_path: Path) -> None:
    f = _write_tests(
        tmp_path,
        _HEADER + '''

def test_a(pom: LoginPage) -> None:
    raise NotImplementedError("Implement step: a")
''',
    )
    stubs = find_stubs_in_file(f)
    with pytest.raises(ValueError, match="broke parse"):
        apply_heal(stubs[0], "pom.navigate(")


def test_applier_idempotent_when_run_via_real_validation(tmp_path: Path) -> None:
    """Body that's already a valid heal target (not a stub) is not
    re-found by the scanner, so a second pass finds nothing."""
    f = _write_tests(
        tmp_path,
        _HEADER + '''

def test_a(pom: LoginPage) -> None:
    raise NotImplementedError("Implement step: a")
''',
    )
    stubs = find_stubs_in_file(f)
    new_text = apply_heal(stubs[0], "pom.navigate()")
    f.write_text(new_text, encoding="utf-8")
    assert find_stubs_in_file(f) == []
