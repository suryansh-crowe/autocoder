"""Centralised settings loader.

Everything the orchestrator needs from the environment lives here as one
typed Settings object. No other module reads ``os.environ`` directly so
callers can be tested with an in-memory Settings instance.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class OllamaSettings:
    endpoint: str
    model: str
    timeout_seconds: int
    num_ctx: int
    temperature: float
    top_p: float
    num_predict: int


@dataclass(frozen=True)
class BrowserSettings:
    headless: bool
    extraction_timeout_ms: int
    extraction_nav_timeout_ms: int
    workers: int


@dataclass(frozen=True)
class ExtractionSettings:
    max_elements_per_page: int
    max_dependency_depth: int


@dataclass(frozen=True)
class Paths:
    project_root: Path
    manifest_dir: Path
    extractions_dir: Path
    plans_dir: Path
    runs_log: Path
    storage_state: Path
    tests_dir: Path
    features_dir: Path
    steps_dir: Path
    pages_dir: Path
    auth_setup_dir: Path
    registry_path: Path


@dataclass(frozen=True)
class Settings:
    """Top-level settings bundle."""

    base_url: str
    login_url: str | None
    log_level: str
    ollama: OllamaSettings
    browser: BrowserSettings
    extraction: ExtractionSettings
    paths: Paths
    secret_env_keys: tuple[str, ...] = field(
        default=(
            "LOGIN_USERNAME",
            "LOGIN_PASSWORD",
            "LOGIN_OTP_SECRET",
            "RBAC_USERNAME",
            "RBAC_PASSWORD",
        )
    )

    def secret_present(self, key: str) -> bool:
        """True if the secret env var has a non-empty value at runtime.

        We never log the value itself, only whether it is present.
        """
        return bool(os.environ.get(key, "").strip())


def load_settings(project_root: Path | None = None) -> Settings:
    """Load settings from the process env (.env if present)."""
    root = (project_root or Path.cwd()).resolve()
    load_dotenv(root / ".env")

    manifest_dir = Path(os.environ.get("AUTOCODER_MANIFEST_DIR", "manifest"))
    if not manifest_dir.is_absolute():
        manifest_dir = root / manifest_dir

    storage_state = Path(os.environ.get("STORAGE_STATE", ".auth/user.json"))
    if not storage_state.is_absolute():
        storage_state = root / storage_state

    tests_dir = root / "tests"
    paths = Paths(
        project_root=root,
        manifest_dir=manifest_dir,
        extractions_dir=manifest_dir / "extractions",
        plans_dir=manifest_dir / "plans",
        runs_log=manifest_dir / "runs.log",
        storage_state=storage_state,
        tests_dir=tests_dir,
        features_dir=tests_dir / "features",
        steps_dir=tests_dir / "steps",
        pages_dir=tests_dir / "pages",
        auth_setup_dir=tests_dir / "auth_setup",
        registry_path=manifest_dir / "registry.yaml",
    )

    return Settings(
        base_url=os.environ.get("BASE_URL", "").rstrip("/"),
        login_url=os.environ.get("LOGIN_URL") or None,
        log_level=os.environ.get("LOG_LEVEL", "info").strip().lower() or "info",
        ollama=OllamaSettings(
            endpoint=os.environ.get("OLLAMA_ENDPOINT", "http://localhost:11434").rstrip("/"),
            model=os.environ.get("OLLAMA_MODEL", "phi4:14b"),
            timeout_seconds=_int("OLLAMA_TIMEOUT_SECONDS", 600),
            num_ctx=_int("OLLAMA_NUM_CTX", 12288),
            temperature=_float("OLLAMA_TEMPERATURE", 0.2),
            top_p=_float("OLLAMA_TOP_P", 0.9),
            num_predict=_int("OLLAMA_NUM_PREDICT", 2048),
        ),
        browser=BrowserSettings(
            headless=_flag("HEADLESS", True),
            extraction_timeout_ms=_int("EXTRACTION_TIMEOUT_MS", 20_000),
            extraction_nav_timeout_ms=_int("EXTRACTION_NAV_TIMEOUT_MS", 30_000),
            workers=_int("PW_WORKERS", 2),
        ),
        extraction=ExtractionSettings(
            max_elements_per_page=_int("MAX_ELEMENTS_PER_PAGE", 60),
            max_dependency_depth=_int("MAX_DEPENDENCY_DEPTH", 3),
        ),
        paths=paths,
    )


def ensure_dirs(settings: Settings) -> None:
    """Create on-disk directories the orchestrator writes to."""
    for d in (
        settings.paths.manifest_dir,
        settings.paths.extractions_dir,
        settings.paths.plans_dir,
        settings.paths.tests_dir,
        settings.paths.features_dir,
        settings.paths.steps_dir,
        settings.paths.pages_dir,
        settings.paths.auth_setup_dir,
        settings.paths.storage_state.parent,
    ):
        d.mkdir(parents=True, exist_ok=True)
