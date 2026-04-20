"""Capture pytest failures and turn them into heal targets.

Two entry points:

* :func:`run_pytest_capture` — invoke pytest in a subprocess with a
  JUnit-XML report, parse the report into structured failures.
* :func:`parse_junit_xml` — same parsing, takes a path the user has
  already produced (`pytest --junit-xml=...`).

Output: a list of :class:`PytestFailure` records that the runner
turns into LLM heal calls — one per failure, with the original step
text + current body + Playwright error message in context.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class PytestFailure:
    test_id: str           # `tests/playwright/test_login.py::test_smoke_...`
    test_file: Path        # absolute path to the test file
    test_function: str     # the pytest test function that failed (e.g. test_smoke_login)
    step_function: str = ""   # legacy alias kept for back-compat; populated for bdd layouts
    error_type: str = ""   # `playwright._impl._errors.TimeoutError` etc.
    error_message: str = ""   # one-line summary
    failure_class: str = ""   # heuristic bucket: timeout|disabled|intercepted|wrong_kind|other
    raw_traceback: str = ""


# ---------------------------------------------------------------------------
# Heuristic classification — used to enrich the LLM prompt with hints.
# ---------------------------------------------------------------------------


_FAILURE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Order matters — most specific first.
    ("wrong_kind", re.compile(r'cannot be filled', re.IGNORECASE)),
    ("disabled", re.compile(r'element is not enabled', re.IGNORECASE)),
    ("intercepted", re.compile(r'intercepts pointer events', re.IGNORECASE)),
    ("not_visible", re.compile(r'element is not visible', re.IGNORECASE)),
    ("not_attached", re.compile(r'element is not attached', re.IGNORECASE)),
    ("locator_not_found", re.compile(r'no selector resolved', re.IGNORECASE)),
    ("timeout", re.compile(r'Timeout \d+ms exceeded', re.IGNORECASE)),
]


def classify(error_message: str) -> str:
    for label, pat in _FAILURE_PATTERNS:
        if pat.search(error_message):
            return label
    return "other"


# ---------------------------------------------------------------------------
# Subprocess driver
# ---------------------------------------------------------------------------


def run_pytest_capture(
    *,
    test_paths: list[Path],
    junit_path: Path,
    extra_args: list[str] | None = None,
) -> list[PytestFailure]:
    """Run ``pytest`` and return parsed failures.

    The actual test outcome is *ignored* — heal cares only about
    failures, and a green run produces an empty list.
    """
    junit_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        "--tb=short",
        f"--junit-xml={junit_path}",
        *(extra_args or []),
        *(str(p) for p in test_paths),
    ]
    try:
        subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=600)
    except FileNotFoundError as exc:
        raise RuntimeError(f"pytest not on PATH: {exc!s}") from exc
    if not junit_path.exists():
        raise RuntimeError(
            "pytest did not produce a JUnit XML report. "
            "Make sure pytest is installed in the active environment."
        )
    return parse_junit_xml(junit_path)


# ---------------------------------------------------------------------------
# JUnit XML parsing
# ---------------------------------------------------------------------------


# Match a Playwright test function name in a traceback line. Pytest
# writes the frame as either ``in test_<name>`` or ``test_<name>(fixture)``.
_TEST_FN_RE = re.compile(r"\bin\s+(test_[a-z0-9_]+)\b|\b(test_[a-z0-9_]+)\(")


def _test_function_from_traceback(text: str) -> str:
    """Walk the traceback for the deepest test_* function call."""
    candidates: list[str] = []
    for line in text.splitlines():
        for m in _TEST_FN_RE.finditer(line):
            candidates.append(m.group(1) or m.group(2))
    return candidates[-1] if candidates else ""


def _abs_test_file(classname: str, name: str, base: Path) -> Path:
    """Reconstruct an absolute file path from a JUnit `classname`."""
    # pytest classname looks like: `tests.steps.test_login`
    parts = classname.split(".")
    return (base / Path(*parts[:-1]) / f"{parts[-1]}.py").resolve()


def parse_junit_xml(path: Path, *, base: Path | None = None) -> list[PytestFailure]:
    base = base or Path.cwd()
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        raise RuntimeError(f"failed to parse {path}: {exc!s}") from exc
    out: list[PytestFailure] = []
    for case in tree.iter("testcase"):
        # `case.find(...)` may return an Element whose __bool__ is
        # False when it has no child elements (only text). The
        # `or` shortcut would then mis-fall through to the next
        # find. Use explicit None checks.
        failure = case.find("failure")
        if failure is None:
            failure = case.find("error")
        if failure is None:
            continue
        classname = case.attrib.get("classname", "")
        name = case.attrib.get("name", "")
        msg = (failure.attrib.get("message") or "").strip()
        body = (failure.text or "").strip()
        first_line = (msg or body).splitlines()[0] if (msg or body) else ""
        err_type = failure.attrib.get("type", "")
        # Prefer an explicit ``test_*`` frame in the traceback. When the
        # traceback does not mention one (pytest truncated it, custom
        # hook) fall back to the testcase ``name`` attribute, which is
        # the test function name pytest ran.
        test_fn = _test_function_from_traceback(body) or name
        out.append(
            PytestFailure(
                test_id=f"{classname}::{name}",
                test_file=_abs_test_file(classname, name, base),
                test_function=test_fn,
                error_type=err_type,
                error_message=first_line,
                failure_class=classify(body or msg or first_line),
                raw_traceback=body,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Helpers exposed for tests
# ---------------------------------------------------------------------------


def has_pytest() -> bool:
    return shutil.which("pytest") is not None or shutil.which(sys.executable) is not None
