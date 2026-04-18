"""Tests for `autocoder.extract.inspector._kind_for` — element-kind mapping.

Why this matters: the catalog `kind` drives the planner's choice of
Playwright primitive in the POM (`fill` / `check` / `click` / etc.).
A checkbox labelled `kind=input` produces a POM that calls `.fill()`
on it — which Playwright rejects at runtime ("Input of type
checkbox cannot be filled"). The fix is to honour the resolved
ARIA role first.
"""

from __future__ import annotations

import pytest

from autocoder.extract.inspector import _kind_for


@pytest.mark.parametrize(
    "role, tag, expected",
    [
        # Role wins: an <input type=checkbox> resolves to role=checkbox
        # via selectors.py, which then drives kind=checkbox here.
        ("checkbox", "input", "checkbox"),
        ("radio", "input", "radio"),
        ("button", "input", "button"),
        # Free-text inputs keep kind=input (planner emits .fill()).
        ("textbox", "input", "input"),
        (None, "input", "input"),
        # Buttons by tag and role both map to button.
        ("button", "button", "button"),
        (None, "button", "button"),
        # Anchors → link.
        ("link", "a", "link"),
        (None, "a", "link"),
        # Selects.
        (None, "select", "select"),
        ("combobox", "div", "select"),
        # Tabs / menu items.
        ("tab", "div", "tab"),
        ("menuitem", "div", "menuitem"),
        # Textareas.
        (None, "textarea", "textarea"),
        # Catch-all.
        (None, "div", "other"),
        ("", "", "other"),
    ],
)
def test_kind_for_returns_expected_label(role, tag, expected):
    assert _kind_for(role, tag) == expected


def test_checkbox_role_overrides_input_tag():
    """REGRESSION: a `<input type=checkbox>` previously got kind=input
    because the function checked `tag == 'input'` before any role
    rules. The planner then emitted `.fill()` on it, which Playwright
    rejects with `Input of type "checkbox" cannot be filled`."""
    assert _kind_for("checkbox", "input") == "checkbox"


def test_radio_role_overrides_input_tag():
    assert _kind_for("radio", "input") == "radio"


def test_button_role_overrides_input_tag():
    """`<input type=submit>` → role=button via selectors._role; kind
    must follow."""
    assert _kind_for("button", "input") == "button"
