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

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
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
class ReportData:
    slugs: list[SlugReport]
    total_scenarios: int
    total_passed: int
    total_failed: int
    total_unknown: int


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
    runs_dir = settings.paths.manifest_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    for slug in slugs:
        test_file = settings.paths.steps_dir / f"test_{slug}.py"
        if not test_file.exists():
            logger.warn("report_pytest_skipped_missing", slug=slug, path=str(test_file))
            continue
        junit_path = runs_dir / f"{slug}.xml"
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

    When ``run_pytest`` is true, pytest is executed for every slug that
    has a ``tests/steps/test_<slug>.py`` file, producing fresh JUnit
    XML under ``manifest/runs/``. When false, existing JUnit files are
    used (scenarios without a JUnit record are marked *unknown*).
    """
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
        for p in settings.paths.steps_dir.glob("test_*.py"):
            slug_to_url[p.stem.removeprefix("test_")] = ""
    slug_list = sorted(slug_to_url.keys())

    if run_pytest:
        _run_pytest_for_slugs(settings, slug_list)

    out: list[SlugReport] = []
    total_pass = total_fail = total_unknown = 0
    for slug in slug_list:
        url = slug_to_url.get(slug, "")
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

    return ReportData(
        slugs=out,
        total_scenarios=total_pass + total_fail + total_unknown,
        total_passed=total_pass,
        total_failed=total_fail,
        total_unknown=total_unknown,
    )
