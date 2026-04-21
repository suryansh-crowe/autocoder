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
class AzureOpenAISettings:
    """Config for the Azure OpenAI hosted backend.

    Activated by setting ``USE_AZURE_OPENAI=true`` in ``.env``. Every
    field has an environment override; defaults produce Azure's
    current GA chat completions shape.

    Only the **name** of the env var that holds the API key is stored
    on this dataclass — never the key itself — so the key cannot leak
    through logs or registry yaml.
    """

    endpoint: str                # e.g. https://<resource>.openai.azure.com
    api_key_env: str             # name of the env var holding the key
    api_version: str
    deployment: str              # Azure deployment name (not the base model)
    temperature: float
    top_p: float
    max_tokens: int
    timeout_seconds: int


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
    logs_dir: Path
    storage_state: Path
    tests_dir: Path
    features_dir: Path
    steps_dir: Path
    pages_dir: Path
    generated_dir: Path
    auth_setup_dir: Path
    registry_path: Path


@dataclass(frozen=True)
class Settings:
    """Top-level settings bundle."""

    base_url: str
    login_url: str | None
    log_level: str
    # LLM backend selection.
    #
    # ``use_azure_openai=True`` routes every planner call (POM plan,
    # feature plan, stub heal, failure heal) to the Azure OpenAI
    # deployment in ``azure_openai``. The Ollama deployment in
    # ``ollama`` is still read so you can flip the switch back in
    # ``.env`` without editing code.
    use_azure_openai: bool
    ollama: OllamaSettings
    azure_openai: AzureOpenAISettings
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
            "OPENAI_API_KEY",
            "AZURE_OPENAI_API_KEY",
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
        logs_dir=manifest_dir / "logs",
        storage_state=storage_state,
        tests_dir=tests_dir,
        features_dir=tests_dir / "features",
        steps_dir=tests_dir / "steps",
        pages_dir=tests_dir / "pages",
        generated_dir=tests_dir / "generated",
        auth_setup_dir=tests_dir / "auth_setup",
        registry_path=manifest_dir / "registry.yaml",
    )

    # The Azure API key has two accepted env var names because SDKs
    # disagree. We read whichever is set and remember the name (never
    # the value) so logs and manifest yaml can show presence only.
    azure_key_env = (
        "AZURE_OPENAI_API_KEY"
        if os.environ.get("AZURE_OPENAI_API_KEY", "").strip()
        else "OPENAI_API_KEY"
    )

    return Settings(
        base_url=os.environ.get("BASE_URL", "").rstrip("/"),
        login_url=os.environ.get("LOGIN_URL") or None,
        log_level=os.environ.get("LOG_LEVEL", "info").strip().lower() or "info",
        use_azure_openai=_flag("USE_AZURE_OPENAI", False),
        ollama=OllamaSettings(
            endpoint=os.environ.get("OLLAMA_ENDPOINT", "http://localhost:11434").rstrip("/"),
            model=os.environ.get("OLLAMA_MODEL", "phi4:14b"),
            timeout_seconds=_int("OLLAMA_TIMEOUT_SECONDS", 600),
            num_ctx=_int("OLLAMA_NUM_CTX", 12288),
            temperature=_float("OLLAMA_TEMPERATURE", 0.2),
            top_p=_float("OLLAMA_TOP_P", 0.9),
            num_predict=_int("OLLAMA_NUM_PREDICT", 2048),
        ),
        azure_openai=AzureOpenAISettings(
            endpoint=(
                os.environ.get("AZURE_OPENAI_ENDPOINT")
                or os.environ.get("OPENAI_ENDPOINT", "")
            ).rstrip("/"),
            api_key_env=azure_key_env,
            api_version=os.environ.get("OPENAI_API_VERSION", "2024-12-01-preview"),
            deployment=os.environ.get("AZURE_CHAT_DEPLOYMENT", "gpt-4.1"),
            temperature=_float("AZURE_CHAT_TEMPERATURE", 0.2),
            top_p=_float("AZURE_CHAT_TOP_P", 0.9),
            max_tokens=_int("AZURE_CHAT_MAX_TOKENS", 2048),
            timeout_seconds=_int("AZURE_CHAT_TIMEOUT_SECONDS", 120),
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


RUN_FOLDER_PREFIX = "generated_"


def run_dir_for(settings: Settings, run_stamp: str) -> Path:
    """Return ``tests/generated/generated_<run_stamp>/`` — the per-run
    folder that holds every slug bundle emitted during one orchestrator
    invocation.
    """
    return settings.paths.generated_dir / f"{RUN_FOLDER_PREFIX}{run_stamp}"


def bundle_dir_for(settings: Settings, slug: str, run_stamp: str) -> Path:
    """Return the per-slug bundle for a given run:
    ``tests/generated/generated_<run_stamp>/<slug>/``.
    """
    return run_dir_for(settings, run_stamp) / slug


def latest_bundle_for(settings: Settings, slug: str) -> Path | None:
    """Return the newest bundle directory that contains ``slug``, or
    ``None`` when no run has produced this slug yet.

    Ordering uses the lexicographic run-folder name, which — because
    the stamp is ``YYYYMMDD_HHMMSS`` — matches chronological order.
    """
    candidates: list[tuple[str, Path]] = []
    for run_folder in settings.paths.generated_dir.glob(f"{RUN_FOLDER_PREFIX}*"):
        if not run_folder.is_dir():
            continue
        bundle = run_folder / slug
        # Require the feature file so a half-written bundle (crash
        # mid-run) does not get picked up as the "latest".
        if (bundle / f"{slug}.feature").exists():
            candidates.append((run_folder.name, bundle))
    if not candidates:
        return None
    candidates.sort(key=lambda p: p[0], reverse=True)
    return candidates[0][1]


def ensure_run_dir(settings: Settings, run_stamp: str) -> Path:
    """Create ``tests/generated/generated_<run_stamp>/`` if missing and
    make it a package so pytest's rootdir walk reaches ``tests/``.
    """
    run_dir = run_dir_for(settings, run_stamp)
    run_dir.mkdir(parents=True, exist_ok=True)
    init_file = run_dir / "__init__.py"
    if not init_file.exists():
        init_file.write_text("", encoding="utf-8")
    return run_dir


def _rescope_paths_to_manifest_root(paths: "Paths", manifest_root: Path) -> "Paths":
    """Return a new ``Paths`` with every manifest-rooted member pointing
    inside ``manifest_root``. Everything under ``tests/`` is left alone
    because the per-run folder only owns manifest-class state (registry,
    extractions, plans, heals, logs); the generated tests themselves
    live as sibling folders next to ``manifest/``.
    """
    from dataclasses import replace

    return replace(
        paths,
        manifest_dir=manifest_root,
        extractions_dir=manifest_root / "extractions",
        plans_dir=manifest_root / "plans",
        logs_dir=manifest_root / "logs",
        registry_path=manifest_root / "registry.yaml",
    )


def scope_settings_to_run(settings: Settings, run_stamp: str) -> Settings:
    """Return a ``Settings`` whose manifest paths live inside
    ``tests/generated/generated_<run_stamp>/manifest/``.

    Call this at the top of ``run_generate`` so every downstream write
    — registry, extractions, plans, heals, logs — lands inside the
    run folder. The run folder is the ONLY manifest root while the
    run is in progress.
    """
    from dataclasses import replace

    manifest_root = run_dir_for(settings, run_stamp) / "manifest"
    new_paths = _rescope_paths_to_manifest_root(settings.paths, manifest_root)
    return replace(settings, paths=new_paths)


def latest_run_manifest(settings: Settings) -> Path | None:
    """Return the newest ``generated_*/manifest/`` folder under
    ``tests/generated/``, or ``None`` when no run has produced one yet.
    Sort is lexicographic on the run-folder name; because the stamp is
    ``YYYYMMDD_HHMMSS`` that matches chronological order.
    """
    for run_folder in sorted(
        settings.paths.generated_dir.glob(f"{RUN_FOLDER_PREFIX}*"), reverse=True
    ):
        candidate = run_folder / "manifest"
        if candidate.is_dir():
            return candidate
    return None


def scope_settings_to_latest_run(settings: Settings) -> Settings:
    """Rescope ``settings`` to read/write against the newest run
    folder's manifest. Used by read-heavy entry points (``autocoder
    heal``, ``autocoder report``) so they read the same registry /
    extractions / plans the generate pass wrote.

    When no run exists yet the caller gets back an unchanged Settings
    pointing at the legacy root-level ``manifest/`` path — which may
    not exist. That is intentional: those commands fail fast with
    "no runs found" rather than silently recreating a stale cache.
    """
    from dataclasses import replace

    latest = latest_run_manifest(settings)
    if latest is None:
        return settings
    new_paths = _rescope_paths_to_manifest_root(settings.paths, latest)
    return replace(settings, paths=new_paths)


def seed_manifest_from_previous_run(settings: Settings, run_stamp: str) -> bool:
    """Copy the most recent prior run's manifest into the new run
    folder's manifest so cache-by-fingerprint hits keep skipping the
    LLM. Returns True when a seed was copied, False when there was no
    prior run.

    Must be called BEFORE :func:`scope_settings_to_run` because it
    reads from the *previous* run and writes into the path the new
    scope will point at.
    """
    import shutil

    new_manifest = run_dir_for(settings, run_stamp) / "manifest"
    if new_manifest.exists():
        # Fresh run folder should not already have a manifest; bail
        # rather than silently merging into a partial snapshot.
        return False

    # Find the newest prior manifest (exclude the run folder we are
    # about to write into — it may not exist yet anyway).
    new_run_name = f"{RUN_FOLDER_PREFIX}{run_stamp}"
    for run_folder in sorted(
        settings.paths.generated_dir.glob(f"{RUN_FOLDER_PREFIX}*"), reverse=True
    ):
        if run_folder.name == new_run_name:
            continue
        prior = run_folder / "manifest"
        if prior.is_dir():
            shutil.copytree(prior, new_manifest)
            return True
    return False


def ensure_dirs(settings: Settings) -> None:
    """Create on-disk directories the orchestrator writes to."""
    for d in (
        settings.paths.manifest_dir,
        settings.paths.extractions_dir,
        settings.paths.plans_dir,
        settings.paths.logs_dir,
        settings.paths.tests_dir,
        settings.paths.features_dir,
        settings.paths.steps_dir,
        settings.paths.pages_dir,
        settings.paths.generated_dir,
        settings.paths.auth_setup_dir,
        settings.paths.storage_state.parent,
    ):
        d.mkdir(parents=True, exist_ok=True)
    # ``tests/generated/`` must be a package so pytest walks up through
    # it into ``tests/`` — that keeps ``from tests.pages.base_page``,
    # ``from tests.support...`` imports in generated page files working.
    init_file = settings.paths.generated_dir / "__init__.py"
    if not init_file.exists():
        init_file.write_text("", encoding="utf-8")
