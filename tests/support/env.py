"""Tiny helpers for tests to read env vars safely.

Generated tests should call :func:`require` instead of ``os.environ[...]``
so a missing secret produces a clear error message rather than a
``KeyError`` deep in the call stack.
"""

from __future__ import annotations

import os


def require(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(f"Required env var '{name}' is not set. Add it to .env (never commit it).")
    return val


def optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()
