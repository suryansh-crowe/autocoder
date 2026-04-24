"""Project-level pytest configuration.

The only job of this file is to guarantee that ``.env`` is loaded
before test collection starts. The *actual* loading — and every
typed constant derived from the environment — lives in
:mod:`tests.settings`, which is the single source of truth for every
module under ``tests/``.

Importing ``tests.settings`` here triggers its module-level
``load_dotenv`` exactly once. Subsequent imports of the same module
are cached, so every fixture, generated test, and support helper
sees the same values.
"""

from __future__ import annotations

from tests import settings  # noqa: F401 — loads .env at import time
