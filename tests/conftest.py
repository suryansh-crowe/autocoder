"""Pytest fixtures for the generated test suite.

Two distinct ``page`` fixtures are exposed:

* ``page``      — uses ``storage_state`` if it exists, so the test
                  starts authenticated. This is the default.
* ``raw_page``  — fresh context with no storage_state. Use it for
                  the auth-setup test or any explicitly-anonymous
                  scenario.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest
from playwright.sync_api import Browser, BrowserContext, Page


def _storage_state_path() -> Path:
    raw = os.environ.get("STORAGE_STATE", ".auth/user.json")
    p = Path(raw)
    if not p.is_absolute():
        p = Path.cwd() / p
    return p


@pytest.fixture(scope="session")
def base_url() -> str:
    return os.environ.get("BASE_URL", "").rstrip("/")


@pytest.fixture
def browser_context_args(browser_context_args, base_url):  # noqa: F811
    storage_state = _storage_state_path()
    args = dict(browser_context_args)
    if base_url:
        args["base_url"] = base_url
    if storage_state.exists() and storage_state.stat().st_size > 0:
        args["storage_state"] = str(storage_state)
    return args


@pytest.fixture
def raw_context(browser: Browser, base_url) -> Iterator[BrowserContext]:
    """Fresh context, ignores any saved storage_state."""
    kwargs: dict = {}
    if base_url:
        kwargs["base_url"] = base_url
    context = browser.new_context(**kwargs)
    try:
        yield context
    finally:
        context.close()


@pytest.fixture
def raw_page(raw_context: BrowserContext) -> Iterator[Page]:
    page = raw_context.new_page()
    try:
        yield page
    finally:
        page.close()
