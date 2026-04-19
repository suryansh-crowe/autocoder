"""Pytest fixtures for the generated test suite.

Two distinct ``page`` fixtures are exposed:

* ``page``      — uses ``storage_state`` if it exists, so the test
                  starts authenticated. This is the default.
* ``raw_page``  — fresh context with no storage_state. Use it for
                  the auth-setup test or any explicitly-anonymous
                  scenario.

The module also bridges two env vars from ``.env`` into the
``pytest-playwright`` plugin so the autocoder side and the pytest
side stay in sync:

* ``HEADLESS``                 -> browser launch headed/headless mode
* ``PW_SLOWMO_MS``             -> per-action slow-motion delay in ms
                                  (useful when ``HEADLESS=false`` so
                                  you can watch the flow)

Priority: an explicit ``pytest --headed`` / ``--headless`` flag
always wins over the env var. This lets CI override ``.env``
without editing files.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest
from playwright.sync_api import Browser, BrowserContext, Page


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        return int(raw)
    except ValueError:
        return default


def _storage_state_path() -> Path:
    raw = os.environ.get("STORAGE_STATE", ".auth/user.json")
    p = Path(raw)
    if not p.is_absolute():
        p = Path.cwd() / p
    return p


@pytest.fixture(scope="session")
def base_url() -> str:
    return os.environ.get("BASE_URL", "").rstrip("/")


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):  # noqa: F811
    """Bridge ``HEADLESS`` and ``PW_SLOWMO_MS`` from .env into pytest-playwright.

    By default ``pytest-playwright`` only honours its own ``--headed``
    flag — it does **not** read the ``HEADLESS`` env var. That's why
    ``pytest tests/steps`` is silent even when ``.env`` has
    ``HEADLESS=false``. This fixture closes that gap.

    An explicit ``--headed`` / ``--headless`` CLI flag still wins,
    because those flags mutate ``browser_type_launch_args`` before
    this fixture runs.
    """
    args = dict(browser_type_launch_args)
    # Respect CLI overrides: if the key was set by --headed/--headless
    # handling upstream, leave it alone.
    if "headless" not in browser_type_launch_args:
        args["headless"] = _env_flag("HEADLESS", True)
    slowmo = _env_int("PW_SLOWMO_MS", 0)
    if slowmo > 0 and "slow_mo" not in browser_type_launch_args:
        args["slow_mo"] = slowmo
    return args


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
