"""Consolidated coverage + execution report.

Produces a single view of:

* **UI components** detected on every URL (inventory from extractions).
* **Generated scenarios** (titles + tier tags from each feature file).
* **Execution results** per scenario (pass/fail) — parsed from JUnit
  XML reports under ``manifest/runs/<slug>.xml``, optionally produced
  by running pytest on the generated step files first.
* **Overall summary** (URLs × scenarios × pass/fail totals).

The CLI entry point is :func:`run_report`; ``autocoder report`` wires
it up in :mod:`autocoder.cli`.
"""

from __future__ import annotations

import html as _html
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from autocoder import logger
from autocoder.config import Settings, ensure_dirs
from autocoder.heal.pytest_failures import run_pytest_capture
from autocoder.llm.prompts import build_ui_inventory
from autocoder.models import PageExtraction
from autocoder.registry.store import RegistryStore


@dataclass
class ScenarioRow:
    slug: str
    title: str
    tiers: list[str]
    passed: bool | None = None
    error: str = ""


@dataclass
class SlugReport:
    slug: str
    url: str
    inventory: dict
    scenarios: list[ScenarioRow] = field(default_factory=list)
    feature_path: Path | None = None
    steps_path: Path | None = None
    junit_path: Path | None = None


@dataclass
class DefectRow:
    """One frontend-classified failure surfaced by the heal engine."""

    slug: str
    test_id: str
    step_function: str
    error_type: str
    error_message: str
    failure_class: str
    element_id: str


@dataclass
class ReportData:
    slugs: list[SlugReport]
    total_scenarios: int
    total_passed: int
    total_failed: int
    total_unknown: int
    defects: list[DefectRow] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Feature file parser — titles + tier tags only (no gherkin dep)
# ---------------------------------------------------------------------------


_SCENARIO_RE = re.compile(r"^\s*Scenario(?:\s+Outline)?:\s*(.+?)\s*$")
_TAG_RE = re.compile(r"@([A-Za-z0-9_\-]+)")


def _parse_feature_file(path: Path) -> list[tuple[str, list[str]]]:
    """Return ``[(title, tiers), ...]`` from a .feature file.

    Tiers are the ``@<name>`` tag tokens on the line(s) immediately
    preceding the Scenario header. Everything else in the file is
    ignored.
    """
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[tuple[str, list[str]]] = []
    pending_tags: list[str] = []
    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("@"):
            pending_tags = _TAG_RE.findall(stripped)
            continue
        m = _SCENARIO_RE.match(line)
        if m:
            out.append((m.group(1), list(pending_tags)))
            pending_tags = []
            continue
        # Any non-tag, non-scenario line resets pending tags unless it
        # is the Feature/Background header — we don't need to track
        # those explicitly since we only emit rows on Scenario lines.
        if not stripped.startswith("#"):
            pending_tags = []
    return out


# ---------------------------------------------------------------------------
# JUnit parser — per-scenario pass/fail
# ---------------------------------------------------------------------------


def _scenario_title_from_testcase(name: str) -> str:
    """pytest-bdd emits testcase name like ``test_search_for_assets_in_catalog``.

    We strip the ``test_`` prefix, unescape underscores, and title-case
    each word so the display matches the feature file's scenario
    title as closely as possible.
    """
    if name.startswith("test_"):
        name = name[len("test_") :]
    return name.replace("_", " ").strip().lower()


def _norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _parse_junit(path: Path) -> dict[str, tuple[bool, str]]:
    """Return ``{normalised_scenario_title: (passed, error_text)}``."""
    if not path.exists():
        return {}
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return {}
    out: dict[str, tuple[bool, str]] = {}
    for case in tree.iter("testcase"):
        name = case.attrib.get("name", "")
        title_norm = _norm_title(_scenario_title_from_testcase(name))
        failure = case.find("failure")
        if failure is None:
            failure = case.find("error")
        if failure is None:
            out[title_norm] = (True, "")
        else:
            msg = (failure.attrib.get("message") or "").strip()
            body = (failure.text or "").strip()
            first = (msg or body).splitlines()[0] if (msg or body) else ""
            out[title_norm] = (False, first)
    return out


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------


def _load_extraction_inventory(settings: Settings, slug: str) -> dict:
    path = settings.paths.extractions_dir / f"{slug}.json"
    if not path.exists():
        return {}
    try:
        extraction = PageExtraction.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}
    return build_ui_inventory(extraction)


def _run_pytest_for_slugs(settings: Settings, slugs: list[str]) -> None:
    from autocoder.config import latest_bundle_for

    for slug in slugs:
        bundle = latest_bundle_for(settings, slug)
        if bundle is not None:
            test_file = bundle / f"test_{slug}.py"
            junit_path = bundle / "results.xml"
        else:
            # Legacy flat layout fallback.
            test_file = settings.paths.steps_dir / f"test_{slug}.py"
            bundle = test_file.parent
            junit_dir = settings.paths.manifest_dir / "runs"
            junit_dir.mkdir(parents=True, exist_ok=True)
            junit_path = junit_dir / f"{slug}.xml"
        if not test_file.exists():
            logger.warn("report_pytest_skipped_missing", slug=slug, path=str(test_file))
            continue
        bundle.mkdir(parents=True, exist_ok=True)
        logger.info(
            "report_pytest_run",
            slug=slug,
            target=str(test_file),
            junit=str(junit_path),
        )
        try:
            run_pytest_capture(test_paths=[test_file], junit_path=junit_path)
        except Exception as exc:  # noqa: BLE001
            logger.error("report_pytest_failed", slug=slug, err=str(exc))


def build_report(settings: Settings, *, run_pytest: bool) -> ReportData:
    """Produce the consolidated report data.

    When ``run_pytest`` is true, pytest is executed for every slug
    whose newest bundle has a ``test_<slug>.py`` file, producing a
    fresh ``tests/generated/<run>/<slug>/results.xml`` in place. When
    false, existing JUnit files are used (scenarios without a JUnit
    record are marked *unknown*).

    Rescopes ``settings`` so registry / extractions read from the
    newest run folder's ``manifest/``.
    """
    from autocoder.config import scope_settings_to_latest_run

    settings = scope_settings_to_latest_run(settings)
    ensure_dirs(settings)
    store = RegistryStore(settings.paths.registry_path)
    registry = store.load()

    # ``registry.nodes`` is keyed by URL; we need slugs. Fall back to
    # the filesystem when the registry is empty.
    slug_to_url: dict[str, str] = {}
    for node in registry.nodes.values():
        if node.slug:
            slug_to_url[node.slug] = node.url
    if not slug_to_url:
        # Prefer the new per-run-folder bundle layout; fall back to legacy flat.
        for p in settings.paths.generated_dir.glob("generated_*/*/test_*.py"):
            slug_to_url[p.stem.removeprefix("test_")] = ""
        if not slug_to_url:
            for p in settings.paths.steps_dir.glob("test_*.py"):
                slug_to_url[p.stem.removeprefix("test_")] = ""
    slug_list = sorted(slug_to_url.keys())

    if run_pytest:
        _run_pytest_for_slugs(settings, slug_list)

    out: list[SlugReport] = []
    total_pass = total_fail = total_unknown = 0
    for slug in slug_list:
        url = slug_to_url.get(slug, "")
        from autocoder.config import latest_bundle_for

        bundle = latest_bundle_for(settings, slug)
        if bundle is not None:
            feature_path = bundle / f"{slug}.feature"
            steps_path = bundle / f"test_{slug}.py"
            junit_path = bundle / "results.xml"
        else:
            # Legacy flat layout fallback.
            feature_path = settings.paths.features_dir / f"{slug}.feature"
            steps_path = settings.paths.steps_dir / f"test_{slug}.py"
            junit_path = settings.paths.manifest_dir / "runs" / f"{slug}.xml"

        scenarios_raw = _parse_feature_file(feature_path)
        results = _parse_junit(junit_path)

        scenarios: list[ScenarioRow] = []
        for title, tiers in scenarios_raw:
            key = _norm_title(title)
            passed = None
            error = ""
            if results:
                # Direct hit, else best prefix / contains match.
                hit = results.get(key)
                if hit is None:
                    for rk, rv in results.items():
                        if rk and (rk in key or key in rk):
                            hit = rv
                            break
                if hit is not None:
                    passed, error = hit
            scenarios.append(
                ScenarioRow(
                    slug=slug,
                    title=title,
                    tiers=tiers,
                    passed=passed,
                    error=error,
                )
            )
            if passed is True:
                total_pass += 1
            elif passed is False:
                total_fail += 1
            else:
                total_unknown += 1

        out.append(
            SlugReport(
                slug=slug,
                url=url,
                inventory=_load_extraction_inventory(settings, slug),
                scenarios=scenarios,
                feature_path=feature_path if feature_path.exists() else None,
                steps_path=steps_path if steps_path.exists() else None,
                junit_path=junit_path if junit_path.exists() else None,
            )
        )

    defects = _load_defects(settings)

    return ReportData(
        slugs=out,
        total_scenarios=total_pass + total_fail + total_unknown,
        total_passed=total_pass,
        total_failed=total_fail,
        total_unknown=total_unknown,
        defects=defects,
    )


def write_html_reports(
    settings: Settings,
    data: ReportData,
    *,
    explicit_path: Path | None = None,
) -> tuple[Path, Path | None]:
    """Write the HTML report. Always keeps two copies on disk:

    * ``manifest/reports/report-<YYYYMMDD-HHMMSS>.html`` — timestamped,
      preserved across runs so you can compare historical runs.
    * ``manifest/report.html`` — well-known "latest" path, same
      content as the newest timestamped file, overwritten every run.

    When ``explicit_path`` is given (``autocoder report --html <path>``),
    that path is written instead of the timestamped file; the latest
    symlink is still updated so tooling pointing at
    ``manifest/report.html`` keeps working.

    Returns ``(latest_path, timestamped_path_or_None)``.
    """
    latest = settings.paths.manifest_dir / "report.html"
    latest.parent.mkdir(parents=True, exist_ok=True)
    reports_dir = settings.paths.manifest_dir / "reports"

    # Build a list of prior timestamped reports (relative hrefs,
    # newest first) that the new HTML can link to. Empty on the
    # first run.
    prior: list[tuple[str, str]] = []
    if reports_dir.is_dir():
        for p in sorted(
            reports_dir.glob("report-*.html"),
            key=lambda q: q.stat().st_mtime,
            reverse=True,
        )[:25]:
            label = p.stem  # e.g. ``report-20260420-221944``
            prior.append((label, f"reports/{p.name}"))

    if explicit_path is not None:
        html = render_html_report(
            data, report_id=explicit_path.stem, prior_reports=prior
        )
        explicit_path.parent.mkdir(parents=True, exist_ok=True)
        explicit_path.write_text(html, encoding="utf-8")
        latest.write_text(html, encoding="utf-8")
        logger.ok(
            "report_html_written",
            latest=str(latest),
            path=str(explicit_path),
            source="explicit",
        )
        return latest, explicit_path

    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    timestamped = reports_dir / f"report-{stamp}.html"
    html = render_html_report(data, report_id=timestamped.stem, prior_reports=prior)
    timestamped.write_text(html, encoding="utf-8")
    latest.write_text(html, encoding="utf-8")
    logger.ok(
        "report_html_written",
        latest=str(latest),
        timestamped=str(timestamped),
        source="auto",
        prior_count=len(prior),
    )
    return latest, timestamped


def _load_defects(settings: Settings) -> list[DefectRow]:
    """Read ``manifest/runs/defects.json`` written by the heal engine."""
    path = settings.paths.manifest_dir / "runs" / "defects.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[DefectRow] = []
    for slug, rows in data.items():
        for row in rows:
            out.append(
                DefectRow(
                    slug=str(slug),
                    test_id=row.get("test_id", ""),
                    step_function=row.get("step_function", ""),
                    error_type=row.get("error_type", ""),
                    error_message=row.get("error_message", ""),
                    failure_class=row.get("failure_class", ""),
                    element_id=row.get("element_id", ""),
                )
            )
    return out


# ---------------------------------------------------------------------------
# HTML renderer
# ---------------------------------------------------------------------------


def _fmt_inventory_html(inv: dict) -> str:
    if not inv:
        return "<span class='muted'>-</span>"
    chips: list[str] = []
    labels = [
        ("search", "search"),
        ("chat", "chat"),
        ("forms", "forms"),
        ("nav", "nav"),
        ("buttons", "buttons"),
        ("choices", "choices"),
        ("data", "data"),
        ("pagination", "pagination"),
        ("submits", "submits"),
    ]
    for key, label in labels:
        val = inv.get(key)
        if isinstance(val, list):
            if val:
                chips.append(
                    f"<span class='chip chip-{key}'>{label}={len(val)}</span>"
                )
        elif isinstance(val, int) and val:
            chips.append(f"<span class='chip chip-{key}'>{label}={val}</span>")
    return "".join(chips) or "<span class='muted'>-</span>"


_CSS = """
:root {
  --bg: #0f172a; --fg: #e2e8f0; --muted: #64748b;
  --card: #1e293b; --border: #334155;
  --pass: #22c55e; --fail: #ef4444; --unknown: #eab308;
  --accent: #38bdf8;
}
* { box-sizing: border-box; }
body { margin: 0; padding: 24px 32px; background: var(--bg); color: var(--fg);
  font: 14px/1.5 -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; }
h1 { margin: 0 0 4px 0; font-size: 22px; letter-spacing: 0.3px; }
.subtitle { color: var(--muted); margin-bottom: 24px; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 12px; margin-bottom: 24px; }
.card { background: var(--card); border: 1px solid var(--border); border-radius: 8px;
  padding: 14px 16px; }
.card .k { color: var(--muted); font-size: 12px; text-transform: uppercase;
  letter-spacing: 0.5px; }
.card .v { font-size: 22px; font-weight: 600; margin-top: 4px; }
.pass { color: var(--pass); }
.fail { color: var(--fail); }
.unknown { color: var(--unknown); }
table { width: 100%; border-collapse: collapse; background: var(--card);
  border: 1px solid var(--border); border-radius: 8px; overflow: hidden;
  margin-bottom: 24px; }
th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid var(--border);
  vertical-align: top; }
th { background: #111827; color: var(--muted); font-weight: 500;
  text-transform: uppercase; font-size: 11px; letter-spacing: 0.5px; }
tr:last-child td { border-bottom: none; }
tbody tr:hover { background: rgba(56, 189, 248, 0.05); }
.chip { display: inline-block; background: rgba(56, 189, 248, 0.15);
  color: var(--accent); border: 1px solid rgba(56, 189, 248, 0.35);
  padding: 2px 8px; border-radius: 999px; font-size: 11px; margin: 2px 4px 2px 0; }
.chip-search { background: rgba(34, 197, 94, 0.15); color: var(--pass);
  border-color: rgba(34, 197, 94, 0.35); }
.chip-chat { background: rgba(245, 158, 11, 0.15); color: #f59e0b;
  border-color: rgba(245, 158, 11, 0.35); }
.chip-forms { background: rgba(244, 114, 182, 0.15); color: #f472b6;
  border-color: rgba(244, 114, 182, 0.35); }
.badge { display: inline-block; padding: 2px 10px; border-radius: 999px;
  font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.3px; }
.badge-pass { background: rgba(34, 197, 94, 0.18); color: var(--pass); }
.badge-fail { background: rgba(239, 68, 68, 0.18); color: var(--fail); }
.badge-unknown { background: rgba(234, 179, 8, 0.18); color: var(--unknown); }
.tier { display: inline-block; background: rgba(148, 163, 184, 0.15);
  color: #cbd5e1; padding: 1px 8px; border-radius: 4px; font-size: 11px;
  margin-right: 4px; font-family: ui-monospace, monospace; }
.muted { color: var(--muted); }
.err { color: #fca5a5; font-family: ui-monospace, SFMono-Regular, monospace;
  font-size: 12px; word-break: break-word; }
.url { color: var(--muted); font-family: ui-monospace, monospace;
  font-size: 12px; word-break: break-all; }
"""


def render_html_report(
    data: ReportData,
    *,
    report_id: str | None = None,
    prior_reports: list[tuple[str, str]] | None = None,
) -> str:
    """Return a standalone HTML dashboard for :class:`ReportData`.

    ``report_id`` is rendered in the header so you can correlate an
    open tab with its on-disk filename (e.g. ``report-20260420-221944``).
    ``prior_reports`` is a list of ``(label, relative_href)`` pairs
    rendered at the bottom so a reader can jump between runs.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    header_id = (
        f" · <code style='color:var(--accent)'>{_html.escape(report_id)}</code>"
        if report_id
        else ""
    )
    pct = (
        f"{100.0 * data.total_passed / data.total_scenarios:.1f}%"
        if data.total_scenarios
        else "-"
    )

    coverage_rows: list[str] = []
    detail_rows: list[str] = []
    for s in data.slugs:
        p = sum(1 for sc in s.scenarios if sc.passed is True)
        f = sum(1 for sc in s.scenarios if sc.passed is False)
        u = sum(1 for sc in s.scenarios if sc.passed is None)
        coverage_rows.append(
            "<tr>"
            f"<td><strong>{_html.escape(s.slug)}</strong><br>"
            f"<span class='url'>{_html.escape(s.url)}</span></td>"
            f"<td>{_fmt_inventory_html(s.inventory)}</td>"
            f"<td>{len(s.scenarios)}</td>"
            f"<td class='pass'>{p}</td>"
            f"<td class='fail'>{f}</td>"
            f"<td class='unknown'>{u}</td>"
            "</tr>"
        )
        for sc in s.scenarios:
            if sc.passed is True:
                result = "<span class='badge badge-pass'>pass</span>"
            elif sc.passed is False:
                result = "<span class='badge badge-fail'>fail</span>"
            else:
                result = "<span class='badge badge-unknown'>unknown</span>"
            tiers = "".join(
                f"<span class='tier'>{_html.escape(t)}</span>" for t in sc.tiers
            ) or "<span class='muted'>-</span>"
            note = (
                f"<div class='err'>{_html.escape(sc.error)}</div>"
                if sc.error
                else ""
            )
            detail_rows.append(
                "<tr>"
                f"<td>{_html.escape(s.slug)}</td>"
                f"<td>{_html.escape(sc.title)}{note}</td>"
                f"<td>{tiers}</td>"
                f"<td>{result}</td>"
                "</tr>"
            )

    defect_rows_html: list[str] = []
    for d in data.defects:
        defect_rows_html.append(
            "<tr>"
            f"<td>{_html.escape(d.slug)}</td>"
            f"<td>{_html.escape(d.step_function or d.test_id)}</td>"
            f"<td><code>{_html.escape(d.element_id) or '<span class=muted>-</span>'}</code></td>"
            f"<td><span class='tier'>{_html.escape(d.failure_class)}</span></td>"
            f"<td><div class='err'>{_html.escape(d.error_message)}</div></td>"
            "</tr>"
        )
    defects_section = ""
    if defect_rows_html:
        defects_section = f"""
<h2>Application defects <span class="chip chip-forms">{len(defect_rows_html)}</span></h2>
<div class="subtitle">
  These failures are classified as real application bugs — the
  referenced element was in the extraction catalog but the running
  app no longer exposes it (or the app returned an HTTP / network
  error). Heal deliberately skipped them so the defect surfaces
  here instead of being masked by a rewritten test.
</div>
<table>
  <thead><tr><th>Slug</th><th>Step / test</th><th>Element</th><th>Class</th><th>Error</th></tr></thead>
  <tbody>
{''.join(defect_rows_html)}
  </tbody>
</table>
"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>autocoder test report</title>
<style>{_CSS}</style>
</head>
<body>
<h1>autocoder — end-to-end test report</h1>
<div class="subtitle">Generated {now}{header_id} · {len(data.slugs)} URLs · {data.total_scenarios} scenarios · pass rate {pct}</div>

<section class="cards">
  <div class="card"><div class="k">URLs</div><div class="v">{len(data.slugs)}</div></div>
  <div class="card"><div class="k">Scenarios</div><div class="v">{data.total_scenarios}</div></div>
  <div class="card"><div class="k">Passed</div><div class="v pass">{data.total_passed}</div></div>
  <div class="card"><div class="k">Failed</div><div class="v fail">{data.total_failed}</div></div>
  <div class="card"><div class="k">Unknown</div><div class="v unknown">{data.total_unknown}</div></div>
  <div class="card"><div class="k">Pass rate</div><div class="v">{pct}</div></div>
</section>

<h2>Per-URL coverage</h2>
<table>
  <thead><tr><th>URL</th><th>Detected UI components</th><th>Scenarios</th><th>Pass</th><th>Fail</th><th>Unknown</th></tr></thead>
  <tbody>
{''.join(coverage_rows)}
  </tbody>
</table>

<h2>Per-scenario results</h2>
<table>
  <thead><tr><th>Slug</th><th>Scenario</th><th>Tiers</th><th>Result</th></tr></thead>
  <tbody>
{''.join(detail_rows)}
  </tbody>
</table>
{defects_section}
{_render_prior_reports_section(prior_reports)}
</body>
</html>
"""


def _render_prior_reports_section(
    prior_reports: list[tuple[str, str]] | None,
) -> str:
    if not prior_reports:
        return ""
    items = "\n".join(
        f'  <li><a href="{_html.escape(href)}">{_html.escape(label)}</a></li>'
        for label, href in prior_reports
    )
    return f"""
<h2>Prior reports</h2>
<div class="subtitle">Jump to a previous run's report. Newest first.</div>
<ul class="prior">
{items}
</ul>
<style>
.prior {{ margin: 0 0 16px; padding: 0 0 0 20px; font-family: ui-monospace, monospace; font-size: 13px; }}
.prior li {{ margin: 2px 0; }}
.prior a {{ color: var(--accent); text-decoration: none; }}
.prior a:hover {{ text-decoration: underline; }}
</style>
"""
