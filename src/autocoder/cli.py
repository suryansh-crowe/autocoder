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
from autocoder.heal import HealOptions, heal_steps
from autocoder.intake.sources import URLSourceError, resolve_urls
from autocoder.orchestrator import (
    DEFAULT_TIERS,
    CycleOutcome,
    GenerateOptions,
    run_extend,
    run_full_cycle,
    run_generate,
    run_status,
)
from autocoder.report import ReportData, SlugReport, build_report


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
    logger.init(settings.paths.logs_dir, level=settings.log_level, command="generate")
    chosen_tiers = list(tiers) or list(DEFAULT_TIERS)
    logger.info(
        "cli_invoke",
        cmd="generate",
        force=force,
        skip_llm=skip_llm,
        tiers=",".join(chosen_tiers),
        log_level=settings.log_level,
        log_file=str(logger.active_log_path() or ""),
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


@cli.command("run")
@click.argument("urls", nargs=-1, required=False)
@click.option(
    "--urls-file",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Plain-text file of URLs (one per line, '#' comments allowed).",
)
@click.option(
    "--tier",
    "tiers",
    multiple=True,
    type=click.Choice(_TIER_CHOICES),
    help="Scenario tiers to generate. Defaults to smoke + happy + validation.",
)
@click.option("--force", is_flag=True, help="Ignore caches and rebuild every artifact.")
@click.option("--skip-llm", is_flag=True, help="Intake + extraction only; do not call the LLM.")
@click.option(
    "--verify/--no-verify",
    default=False,
    show_default=True,
    help="When set, run pytest after generation and heal failures up to "
    "``--max-heal-attempts`` times. Off by default: generation-only is "
    "the normal flow, and healing happens lazily at pytest time via the "
    "autoheal plugin (see ``tests/support/autoheal_plugin.py``).",
)
@click.option(
    "--max-heal-attempts",
    type=click.IntRange(0, 20),
    default=3,
    show_default=True,
    help="Only used with ``--verify``. Upper bound on post-generation heal "
    "passes per failing test file.",
)
def run(
    urls: tuple[str, ...],
    urls_file: Path | None,
    tiers: tuple[str, ...],
    force: bool,
    skip_llm: bool,
    verify: bool,
    max_heal_attempts: int,
) -> None:
    """Generate POM + features + steps for every URL, with self-healing.

    Default behavior (no ``--verify``)
    ----------------------------------
    Runs intake, auth-first (with a shared long-lived browser so MSAL
    state survives into extraction), per-URL extraction, POM/feature/
    steps rendering, and stops. **No pytest is invoked.** This is the
    fast path for iterating on generation itself, and for giving users
    a ready-to-run suite they execute on their own time.

    Heal behavior
    -------------
    Heal happens at pytest time, not generation time. The project ships
    a pytest plugin (``tests/support/autoheal_plugin.py``) that, when
    enabled, watches for step failures, asks the LLM for a revised
    body in-process, patches the step file, and retries — "then and
    there" during the same pytest run. See
    ``readme/17_heal.md`` for the toggle.

    Opt-in verify loop (``--verify``)
    ---------------------------------
    Keeps the older integrated ``generate -> pytest -> heal -> pytest``
    cycle available for CI pipelines that want a one-shot "fail the
    build unless every test passes" check. URLs end in ``verified`` /
    ``needs_implementation`` / ``failed``.
    """
    settings = load_settings()
    logger.init(settings.paths.logs_dir, level=settings.log_level, command="run")
    chosen_tiers = list(tiers) or list(DEFAULT_TIERS)
    logger.info(
        "cli_invoke",
        cmd="run",
        force=force,
        skip_llm=skip_llm,
        verify=verify,
        tiers=",".join(chosen_tiers),
        max_heal_attempts=max_heal_attempts if verify else 0,
        log_level=settings.log_level,
        log_file=str(logger.active_log_path() or ""),
    )
    resolved = _resolve_or_exit(urls, urls_file, settings)
    opts = GenerateOptions(
        urls=resolved,
        tiers=chosen_tiers,
        force=force,
        skip_llm=skip_llm,
    )

    if not verify:
        # Generation only. Self-healing is handled lazily by the
        # pytest autoheal plugin whenever the user runs the suite.
        results = run_generate(settings, opts)
        _print_results(results)
        logger.ok("cli_done", cmd="run", processed=len(results), verify=False)
        _console.print(
            "[cyan]Generation complete.[/] Run the suite with: "
            "[bold]pytest tests/steps[/] "
            "(set ``AUTOCODER_AUTOHEAL=true`` to heal failing steps live)."
        )
        return

    outcome = run_full_cycle(settings, opts, max_heal_attempts=max_heal_attempts)
    _print_cycle_outcome(outcome)
    verified_ct = sum(1 for v in outcome.verification.values() if v.passed)
    still_failing = sum(1 for v in outcome.verification.values() if not v.passed)
    logger.ok(
        "cli_done",
        cmd="run",
        processed=len(outcome.generation),
        verified=verified_ct,
        still_failing=still_failing,
    )
    if still_failing:
        sys.exit(1)


@cli.command("rerun")
@click.option("--force", is_flag=True, help="Ignore caches and rebuild every artifact.")
def rerun(force: bool) -> None:
    """Reprocess every URL already in the registry."""
    settings = load_settings()
    logger.init(settings.paths.logs_dir, level=settings.log_level, command="rerun")
    logger.info(
        "cli_invoke",
        cmd="rerun",
        force=force,
        log_level=settings.log_level,
        log_file=str(logger.active_log_path() or ""),
    )
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
    logger.init(settings.paths.logs_dir, level=settings.log_level, command="extend")
    logger.info(
        "cli_invoke",
        cmd="extend",
        extra_tiers=",".join(extra_tiers),
        log_level=settings.log_level,
        log_file=str(logger.active_log_path() or ""),
    )
    resolved = _resolve_or_exit(urls, urls_file, settings, require=False)
    results = run_extend(settings, resolved, list(extra_tiers))
    _print_results(results)
    logger.ok("cli_done", cmd="extend", processed=len(results))


@cli.command("heal")
@click.option("--slug", default=None, help="Restrict heal to a single URL's step file (test_<slug>.py).")
@click.option("--dry-run", is_flag=True, help="Show what would change without writing files.")
@click.option("--force", is_flag=True, help="Ignore the heal cache and re-call the LLM for every target.")
@click.option(
    "--from-pytest",
    is_flag=True,
    help="Run pytest first; heal step bodies whose test failed at runtime "
    "(uses the failure's error message as extra context).",
)
@click.option(
    "--junit-xml",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Heal from an existing JUnit-XML report instead of running pytest "
    "(`pytest --junit-xml=PATH` produces this).",
)
def heal(
    slug: str | None,
    dry_run: bool,
    force: bool,
    from_pytest: bool,
    junit_xml: Path | None,
) -> None:
    """Auto-fix step bodies via the local LLM.

    Two modes:

    * default — fill `NotImplementedError` stubs the renderer left.
    * `--from-pytest` / `--junit-xml` — heal step bodies whose tests
      failed at runtime, with the Playwright error as context.

    Both modes validate suggestions against the POM's real method
    list and cache results so reruns spend zero tokens unless
    something actually changed.
    """
    settings = load_settings()
    logger.init(settings.paths.logs_dir, level=settings.log_level, command="heal")
    logger.info(
        "cli_invoke",
        cmd="heal",
        slug=slug or "*",
        dry_run=dry_run,
        force=force,
        from_pytest=from_pytest or junit_xml is not None,
        log_level=settings.log_level,
        log_file=str(logger.active_log_path() or ""),
    )
    opts = HealOptions(
        slug=slug,
        dry_run=dry_run,
        force=force,
        from_pytest=from_pytest,
        junit_path=junit_xml,
    )
    results = heal_steps(settings, opts)
    _print_heal_results(results, dry_run=dry_run)
    logger.ok(
        "cli_done",
        cmd="heal",
        total=len(results),
        applied=sum(1 for r in results if r.applied),
    )


@cli.command("report")
@click.option(
    "--run",
    "run_pytest_flag",
    is_flag=True,
    help="Execute pytest for every generated suite before reporting. "
    "Writes fresh JUnit XML into manifest/runs/<slug>.xml.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit the report as machine-readable JSON on stdout instead of rich tables.",
)
@click.option(
    "--html",
    "html_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write a standalone HTML dashboard to this path (open in a browser). "
    "Shows the same coverage + pass/fail data as the terminal view.",
)
@click.option(
    "--open/--no-open",
    "open_html",
    default=True,
    show_default=True,
    help="When writing HTML, automatically open it in the default browser.",
)
def report(
    run_pytest_flag: bool,
    as_json: bool,
    html_path: Path | None,
    open_html: bool,
) -> None:
    """Consolidated coverage + execution report.

    Shows, per URL:

    * the UI components detected (search, chat, nav, forms, buttons…),
    * every generated Gherkin scenario and its tier tags,
    * pass/fail from the most recent pytest run,
    * overall totals for the whole suite.

    Pass ``--run`` to invoke pytest against every generated test file
    first — that guarantees the table reflects the current code on
    disk. Without ``--run``, existing JUnit XML under
    ``manifest/runs/<slug>.xml`` is reused; missing files show as
    ``unknown``.
    """
    settings = load_settings()
    logger.init(settings.paths.logs_dir, level=settings.log_level, command="report")
    logger.info(
        "cli_invoke",
        cmd="report",
        run_pytest=run_pytest_flag,
        json=as_json,
        log_level=settings.log_level,
        log_file=str(logger.active_log_path() or ""),
    )
    data = build_report(settings, run_pytest=run_pytest_flag)
    if html_path is not None:
        from autocoder.report import render_html_report
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(render_html_report(data), encoding="utf-8")
        logger.ok("report_html_written", path=str(html_path))
        _console.print(f"[cyan]HTML report:[/] {html_path}")
        if open_html:
            import webbrowser
            webbrowser.open(html_path.resolve().as_uri())
    if as_json:
        import json as _json
        payload = {
            "total_scenarios": data.total_scenarios,
            "passed": data.total_passed,
            "failed": data.total_failed,
            "unknown": data.total_unknown,
            "failure_category_totals": {
                "frontend": data.total_frontend,
                "script": data.total_script,
                "environment": data.total_environment,
                "other": data.total_other_fail,
            },
            "slugs": [
                {
                    "slug": s.slug,
                    "url": s.url,
                    "inventory": s.inventory,
                    "feature_path": str(s.feature_path) if s.feature_path else None,
                    "steps_path": str(s.steps_path) if s.steps_path else None,
                    "junit_path": str(s.junit_path) if s.junit_path else None,
                    "scenarios": [
                        {
                            "title": sc.title,
                            "tiers": sc.tiers,
                            "passed": sc.passed,
                            "error": sc.error,
                            "failure_class": sc.failure_class,
                            "category": sc.category,
                        }
                        for sc in s.scenarios
                    ],
                }
                for s in data.slugs
            ],
        }
        _console.print_json(_json.dumps(payload))
    else:
        _print_report(data)
    logger.ok(
        "cli_done",
        cmd="report",
        scenarios=data.total_scenarios,
        passed=data.total_passed,
        failed=data.total_failed,
        unknown=data.total_unknown,
    )


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


def _print_cycle_outcome(outcome: CycleOutcome) -> None:
    if not outcome.generation:
        _console.print("[dim]No URLs processed.[/]")
        return

    table = Table(title="autocoder run — full lifecycle", show_lines=False)
    for col in ("slug", "generation", "tests", "heal_attempts", "final", "steps"):
        table.add_column(col, overflow="fold")
    gen_status_by_slug = {r.node.slug: r.node.status.value for r in outcome.generation}
    for r in outcome.generation:
        slug = r.node.slug
        gen = gen_status_by_slug.get(slug, "?")
        verif = outcome.verification.get(slug)
        if verif is None:
            tests_cell = "-"
        elif verif.passed:
            tests_cell = "[green]pass[/]"
        else:
            tests_cell = f"[red]fail ({verif.failure_count})[/]"
        heals = outcome.heal_attempts.get(slug, 0)
        final = outcome.final_status.get(slug, r.node.status).value
        final_style = {
            "verified": "green",
            "complete": "cyan",
            "needs_implementation": "yellow",
            "failed": "red",
        }.get(final, "white")
        table.add_row(
            slug,
            gen,
            tests_cell,
            str(heals),
            f"[{final_style}]{final}[/]",
            _short_path(r.steps_path),
        )
    _console.print(table)


def _print_heal_results(results, *, dry_run: bool) -> None:
    if not results:
        _console.print("[dim]No NotImplementedError stubs found — nothing to heal.[/]")
        return
    title = "heal preview (dry-run)" if dry_run else "heal results"
    table = Table(title=title, show_lines=False)
    for col in ("file", "function", "step", "applied", "cached", "body"):
        table.add_column(col, overflow="fold")
    for r in results:
        applied = "yes" if r.applied else ("dry" if dry_run and not r.issues else "no")
        cached = "yes" if r.cached else "no"
        body = r.suggested_body or ("; ".join(r.issues) if r.issues else "-")
        table.add_row(
            _short_path(r.stub.file_path),
            r.stub.function_name,
            r.stub.step_text,
            applied,
            cached,
            body[:80],
        )
    _console.print(table)


def _fmt_inventory(inv: dict) -> str:
    """One-line summary of detected UI components."""
    if not inv:
        return "-"
    parts: list[str] = []
    labels = [
        ("search", "search"),
        ("chat", "chat"),
        ("forms", "forms"),
        ("nav", "nav"),
        ("buttons", "buttons"),
        ("choices", "choices"),
        ("data", "data"),
    ]
    for key, label in labels:
        val = inv.get(key)
        if isinstance(val, list):
            if val:
                parts.append(f"{label}={len(val)}")
        elif isinstance(val, int) and val:
            parts.append(f"{label}={val}")
    return ", ".join(parts) or "-"


def _print_report(data: ReportData) -> None:
    """Render the consolidated coverage + pass/fail tables."""
    if not data.slugs:
        _console.print("[dim]No URLs in registry or tests directory.[/]")
        return

    # --- 1) Per-URL coverage summary --------------------------------------
    cov = Table(
        title="per-URL coverage (detected UI components + scenario counts)",
        show_lines=False,
    )
    for col in ("slug", "components", "scenarios", "pass", "fail", "unknown"):
        cov.add_column(col, overflow="fold")
    for s in data.slugs:
        p = sum(1 for sc in s.scenarios if sc.passed is True)
        f = sum(1 for sc in s.scenarios if sc.passed is False)
        u = sum(1 for sc in s.scenarios if sc.passed is None)
        cov.add_row(
            s.slug,
            _fmt_inventory(s.inventory),
            str(len(s.scenarios)),
            f"[green]{p}[/]" if p else "0",
            f"[red]{f}[/]" if f else "0",
            f"[yellow]{u}[/]" if u else "0",
        )
    _console.print(cov)

    # --- 2) Per-scenario detail ------------------------------------------
    detail = Table(title="per-scenario results", show_lines=False)
    for col in ("slug", "scenario", "tiers", "result", "note"):
        detail.add_column(col, overflow="fold")
    for s in data.slugs:
        for sc in s.scenarios:
            if sc.passed is True:
                result = "[green]pass[/]"
            elif sc.passed is False:
                result = "[red]fail[/]"
            else:
                result = "[yellow]unknown[/]"
            note = sc.error[:80] if sc.error else ""
            detail.add_row(
                s.slug,
                sc.title,
                ",".join(sc.tiers) or "-",
                result,
                note,
            )
    _console.print(detail)

    # --- 3) Overall summary ----------------------------------------------
    summary = Table(title="overall summary", show_lines=False, show_header=False)
    summary.add_column("metric")
    summary.add_column("value", justify="right")
    summary.add_row("URLs", str(len(data.slugs)))
    summary.add_row("Scenarios", str(data.total_scenarios))
    summary.add_row("[green]Passed[/]", str(data.total_passed))
    summary.add_row("[red]Failed[/]", str(data.total_failed))
    summary.add_row("[yellow]Unknown[/]", str(data.total_unknown))
    if data.total_scenarios:
        pct = 100.0 * data.total_passed / data.total_scenarios
        summary.add_row("Pass rate", f"{pct:.1f}%")
    if data.total_failed:
        summary.add_row(
            "[magenta]Frontend[/] / [cyan]Script[/] / [yellow]Env[/] / Other",
            (
                f"[magenta]{data.total_frontend}[/] / "
                f"[cyan]{data.total_script}[/] / "
                f"[yellow]{data.total_environment}[/] / "
                f"{data.total_other_fail}"
            ),
        )
    _console.print(summary)


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
