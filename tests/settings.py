"""Single source of truth for ``.env`` values on the test side.

Every module under ``tests/`` — the generated POMs, step files, auth
setup, conftest fixtures — imports the constants it needs from this
module. Nothing under ``tests/`` reads ``os.environ`` directly.

Why a dedicated module
----------------------
* Secrets are read exactly once per pytest session. Typos like
  ``LOGINN_URL`` fail at collection time instead of silently
  returning ``None`` at runtime.
* Overriding the whole environment in CI or a containerised run is a
  single change (point ``load_dotenv`` at a different file, or set
  the real env vars before pytest starts).
* The autocoder package has its own equivalent
  (:mod:`autocoder.config`); this file is the test-side twin so
  generated tests can stay importable even without the autocoder
  package on ``sys.path``.

Adding a new knob
-----------------
1. Pick an env var name (upper_snake).
2. Add a typed module-level constant here, using the ``_flag`` /
   ``_int`` / ``_path`` helpers below for anything non-string.
3. Import it from wherever you need it — **never** reach for
   ``os.environ`` under ``tests/`` directly.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


# Resolve the repo root as the parent of ``tests/``. ``.env`` lives
# next to ``pyproject.toml`` / ``pytest.ini`` at the repo root, which
# is one level up from this file.
_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
_ENV_PATH: Path = _REPO_ROOT / ".env"

# ``override=False`` so an explicit ``KEY=value`` exported in the
# shell (CI secrets, docker-compose env) always wins over the file.
load_dotenv(_ENV_PATH, override=False)


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------


def _flag(name: str, default: bool = False) -> bool:
    """Truthy-parse an env var. Accepts the usual shell conventions."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        return int(raw) if raw.strip() else default
    except ValueError:
        return default


def _path(name: str, default_relative: str) -> Path:
    """Read a filesystem path, resolving relative values against the repo root."""
    raw = os.environ.get(name) or default_relative
    p = Path(raw)
    if not p.is_absolute():
        p = (_REPO_ROOT / p).resolve()
    return p


# ---------------------------------------------------------------------------
# Typed constants
# ---------------------------------------------------------------------------

# Application under test
BASE_URL: str = (os.environ.get("BASE_URL") or "").rstrip("/")
LOGIN_URL: str | None = os.environ.get("LOGIN_URL") or None

# Credentials — never hard-coded, never logged. Empty string when absent.
LOGIN_USERNAME: str = os.environ.get("LOGIN_USERNAME", "").strip()
LOGIN_PASSWORD: str = os.environ.get("LOGIN_PASSWORD", "").strip()
LOGIN_OTP_SECRET: str = os.environ.get("LOGIN_OTP_SECRET", "").strip()
RBAC_USERNAME: str = os.environ.get("RBAC_USERNAME", "").strip()
RBAC_PASSWORD: str = os.environ.get("RBAC_PASSWORD", "").strip()

# Auth / session storage
STORAGE_STATE: Path = _path("STORAGE_STATE", ".auth/user.json")
SESSION_STORAGE_COMPANION: Path = STORAGE_STATE.with_name(
    STORAGE_STATE.stem + ".session_storage" + STORAGE_STATE.suffix
)

# Browser controls
HEADLESS: bool = _flag("HEADLESS", True)
PW_SLOWMO_MS: int = _int("PW_SLOWMO_MS", 0)
PW_WORKERS: int = _int("PW_WORKERS", 2)

# Auth runner knobs
AUTH_INTERACTIVE_TIMEOUT_MS: int = _int("AUTH_INTERACTIVE_TIMEOUT_MS", 45_000)

# Autoheal / autoauth toggles
AUTOCODER_AUTOAUTH: bool = _flag("AUTOCODER_AUTOAUTH", bool(LOGIN_URL))
AUTOCODER_AUTOHEAL: bool = _flag("AUTOCODER_AUTOHEAL", False)
AUTOCODER_AUTOHEAL_RERUN: bool = _flag("AUTOCODER_AUTOHEAL_RERUN", False)

# Report + manifest layout
MANIFEST_DIR: Path = _path("AUTOCODER_MANIFEST_DIR", "manifest")
AUTOCODER_AUTOREPORT: bool = _flag("AUTOCODER_AUTOREPORT", True)

# Playwright tracing — record every test, keep only failures on disk.
# Default on so reports have something to link to; turn off with
# ``AUTOCODER_TRACE=false`` in .env when you want faster runs.
AUTOCODER_TRACE: bool = _flag("AUTOCODER_TRACE", True)


# ---------------------------------------------------------------------------
# Convenience helpers for tests that need presence/absence only
# ---------------------------------------------------------------------------


def secret_present(name: str) -> bool:
    """True when an env var has a non-empty value at runtime.

    Only consults the live process env — useful for assertions like
    "skip the form-login test when LOGIN_PASSWORD is unset" without
    importing ``os`` into the test file.
    """
    return bool(os.environ.get(name, "").strip())


def get_required(name: str) -> str:
    """Return a non-empty ``.env`` value or raise a helpful error.

    The generated auth-setup tests call this instead of reading
    ``os.environ`` directly, so every ``.env`` access in the test
    suite funnels through this module.
    """
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(
            f"Missing required env var: {name}. "
            "Add it to .env (never commit the value)."
        )
    return val


def get_optional(name: str, default: str = "") -> str:
    """Return an ``.env`` value stripped, or ``default`` when unset/empty."""
    return os.environ.get(name, default).strip() or default


def storage_state_ready() -> bool:
    """True when a usable session file is on disk."""
    try:
        return STORAGE_STATE.exists() and STORAGE_STATE.stat().st_size > 0
    except OSError:
        return False
