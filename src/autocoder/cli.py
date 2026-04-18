"""Command-line interface.

Usage:

    # First-time generation for a set of URLs
    autocoder generate https://app.example.com/login https://app.example.com/dashboard

    # …or read URLs from a file (one per line, # comments allowed)
    autocoder generate --urls-file urls.txt

    # …or set AUTOCODER_URLS in .env (comma- or newline-separated)
    autocoder generate

    # Re-process the same URLs (uses cached plans + extractions if unchanged)
    autocoder rerun

    # Add new tiers / extend coverage
    autocoder extend --tier regression --tier edge https://app.example.com/dashboard

    # Inspect what the registry currently knows
    autocoder status

URL source priority across `generate` and `extend`:

    CLI args  >  --urls-file  >  $AUTOCODER_URLS

The CLI is intentionally thin — every behaviour lives in
:mod:`autocoder.orchestrator`. This file just parses arguments and
prints a friendly summary.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from autocoder import logger
from autocoder.config import load_settings
from autocoder.intake.sources import URLSourceError, resolve_urls
from autocoder.orchestrator import (
    DEFAULT_TIERS,
    GenerateOptions,
    run_extend,
    run_generate,
    run_status,
)


_console = Console()


_TIER_CHOICES = [
    "smoke",
    "sanity",
    "regression",
    "happy",
    "edge",
    "validation",
    "navigation",
    "auth",
    "rbac",
    "e2e",
]


def _resolve_or_exit(
    cli_urls: tuple[str, ...],
    urls_file: Path | None,
    settings,
    *,
    require: bool = True,
) -> list[str]:
    """Resolve URLs from CLI / file / env / settings fallback.

    Settings fallback uses ``[LOGIN_URL, BASE_URL]`` from .env when no
    explicit URL source is provided — the common case for users who
    only configure the standard env keys.
    """
    settings_fallback = [u for u in (settings.login_url, settings.base_url) if u]
    try:
        resolved = resolve_urls(
            cli_urls=list(cli_urls) if cli_urls else None,
            urls_file=urls_file,
            settings_fallback=settings_fallback,
        )
    except URLSourceError as exc:
        logger.error("urls_invalid", err=str(exc))
        raise click.UsageError(str(exc)) from exc

    if resolved.urls:
        logger.info(
            "urls_source",
            source=resolved.source,
            count=len(resolved.urls),
            sample=",".join(logger.safe_url(u) for u in resolved.urls[:3]),
        )
        return resolved.urls

    if not require:
        logger.info("urls_source", source="none", count=0, fallback="registry")
        return []

    logger.error(
        "urls_missing",
        checked=["cli", "--urls-file", "AUTOCODER_URLS", "BASE_URL/LOGIN_URL"],
    )
    raise click.UsageError(
        "No URLs to process.\n"
        "  - Pass URLs as positional args, or\n"
        "  - Use --urls-file <path> (one URL per line; '#' comments allowed), or\n"
        "  - Set AUTOCODER_URLS in .env (comma- or newline-separated), or\n"
        "  - Set BASE_URL and/or LOGIN_URL in .env."
    )


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli() -> None:
    """URL-driven Playwright BDD test automation."""


@cli.command("generate")
@click.argument("urls", nargs=-1, required=False)
@click.option(
    "--urls-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Plain-text file of URLs (one per line, '#' comments allowed). "
    "Used when no URLs are passed on the CLI.",
)
@click.option(
    "--tier",
    "tiers",
    multiple=True,
    type=click.Choice(_TIER_CHOICES),
    help="Scenario tiers to generate. Repeat to combine. Defaults to smoke + happy + validation.",
)
@click.option("--force", is_flag=True, help="Ignore caches and rebuild every artifact.")
@click.option("--skip-llm", is_flag=True, help="Run intake + extraction only; do not call the LLM.")
def generate(
    urls: tuple[str, ...],
    urls_file: Path | None,
    tiers: tuple[str, ...],
    force: bool,
    skip_llm: bool,
) -> None:
    """Generate POMs, features, and steps for one or more URLs.

    URL source priority: CLI args > --urls-file > $AUTOCODER_URLS.
    """
    settings = load_settings()
    logger.init(settings.paths.runs_log, level=settings.log_level)
    chosen_tiers = list(tiers) or list(DEFAULT_TIERS)
    logger.info(
        "cli_invoke",
        cmd="generate",
        force=force,
        skip_llm=skip_llm,
        tiers=",".join(chosen_tiers),
        log_level=settings.log_level,
    )
    resolved = _resolve_or_exit(urls, urls_file, settings)
    opts = GenerateOptions(
        urls=resolved,
        tiers=chosen_tiers,
        force=force,
        skip_llm=skip_llm,
    )
    results = run_generate(settings, opts)
    _print_results(results)
    logger.ok("cli_done", cmd="generate", processed=len(results))


@cli.command("rerun")
@click.option("--force", is_flag=True, help="Ignore caches and rebuild every artifact.")
def rerun(force: bool) -> None:
    """Reprocess every URL already in the registry."""
    settings = load_settings()
    logger.init(settings.paths.runs_log, level=settings.log_level)
    logger.info("cli_invoke", cmd="rerun", force=force, log_level=settings.log_level)
    registry = run_status(settings)
    if not registry.nodes:
        logger.warn("rerun_no_registry", path=str(settings.paths.registry_path))
        _console.print("[yellow]No URLs in the registry. Run `autocoder generate <urls>` first.[/]")
        sys.exit(2)
    logger.info("rerun_loaded", count=len(registry.nodes))
    opts = GenerateOptions(
        urls=list(registry.nodes.keys()),
        tiers=list(DEFAULT_TIERS),
        force=force,
    )
    results = run_generate(settings, opts)
    _print_results(results)
    logger.ok("cli_done", cmd="rerun", processed=len(results))


@cli.command("extend")
@click.argument("urls", nargs=-1)
@click.option(
    "--urls-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Plain-text file of URLs (one per line, '#' comments allowed). "
    "Used when no URLs are passed on the CLI. "
    "If neither URLs nor a file are given, all registry URLs are extended.",
)
@click.option(
    "--tier",
    "extra_tiers",
    multiple=True,
    required=True,
    type=click.Choice(_TIER_CHOICES[1:]),  # 'smoke' is already the baseline
    help="Additional tiers to add coverage for.",
)
def extend(
    urls: tuple[str, ...],
    urls_file: Path | None,
    extra_tiers: tuple[str, ...],
) -> None:
    """Add coverage tiers to existing URLs.

    URL source priority: CLI args > --urls-file > $AUTOCODER_URLS > entire registry.
    """
    settings = load_settings()
    logger.init(settings.paths.runs_log, level=settings.log_level)
    logger.info(
        "cli_invoke",
        cmd="extend",
        extra_tiers=",".join(extra_tiers),
        log_level=settings.log_level,
    )
    resolved = _resolve_or_exit(urls, urls_file, settings, require=False)
    results = run_extend(settings, resolved, list(extra_tiers))
    _print_results(results)
    logger.ok("cli_done", cmd="extend", processed=len(results))


@cli.command("status")
def status() -> None:
    """Show what the registry currently knows."""
    settings = load_settings()
    registry = run_status(settings)
    if not registry.nodes:
        _console.print("[dim]Registry is empty.[/]")
        return

    table = Table(title="autocoder registry", show_lines=False)
    table.add_column("slug")
    table.add_column("kind")
    table.add_column("status")
    table.add_column("auth?")
    table.add_column("url", overflow="fold")
    for node in registry.nodes.values():
        table.add_row(
            node.slug,
            node.kind.value,
            node.status.value,
            "yes" if node.requires_auth else "no",
            node.url,
        )
    _console.print(table)
    if registry.auth:
        _console.print(
            f"[cyan]auth[/] login_url={registry.auth.login_url} "
            f"setup={'yes' if registry.auth.setup_path else 'no'} "
            f"status={registry.auth.status.value}"
        )


def _print_results(results) -> None:
    if not results:
        _console.print("[dim]No URLs processed.[/]")
        return
    table = Table(title="generated artifacts", show_lines=False)
    for col in ("slug", "status", "pom", "feature", "steps"):
        table.add_column(col)
    for r in results:
        table.add_row(
            r.node.slug,
            r.node.status.value,
            _short_path(r.pom_path),
            _short_path(r.feature_path),
            _short_path(r.steps_path),
        )
    _console.print(table)


def _short_path(path: Path | None) -> str:
    if path is None:
        return "-"
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def main() -> None:
    try:
        cli(standalone_mode=True)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error("cli_error", err=str(exc))
        raise


if __name__ == "__main__":
    main()
