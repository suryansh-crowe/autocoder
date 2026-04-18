"""Project-level pytest configuration.

The Playwright fixtures used by the generated tests live in
``tests/conftest.py`` so generated tests do not need to reach outside
their own folder. This file only sets up shared CLI options.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def pytest_configure(config) -> None:  # noqa: D401
    """Load .env for every pytest run.

    Without this, generated tests that read ``BASE_URL`` /
    ``LOGIN_USERNAME`` straight from ``os.environ`` would only work
    when the user manually exports them.
    """
    load_dotenv(Path(os.getcwd()) / ".env")
