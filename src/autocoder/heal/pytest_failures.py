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
    test_id: str           # `tests/steps/test_login.py::test_smoke_...`
    test_file: Path        # absolute path to the test file
    step_function: str     # the inner step function we land in (e.g. _i_click_X)
    error_type: str        # `playwright._impl._errors.TimeoutError` etc.
    error_message: str     # one-line summary
    failure_class: str     # heuristic bucket: timeout|disabled|intercepted|wrong_kind|other
    # One of:
    # * "script"    — bug in the generated test code. Heal it.
    # * "frontend"  — real application defect. DO NOT heal; report it.
    # * "ambiguous" — signal is unclear either way. Heal conservatively
    #                 (still pass to LLM) but surface on the defect
    #                 report so a human can double-check.
    failure_origin: str = "ambiguous"
    # The element id cited in the error, when one can be extracted.
    # Used by the origin classifier to look up the extraction catalog.
    referenced_element_id: str = ""
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
# Failure-origin classification — script vs. frontend vs. ambiguous.
# ---------------------------------------------------------------------------


# Python-level error types that are (almost) always caused by a bad
# generated body, not by the app under test. Matched against the
# JUnit ``type`` attribute and the first traceback line.
_SCRIPT_ERROR_TYPES = (
    "AttributeError",
    "NameError",
    "SyntaxError",
    "KeyError",
    "ImportError",
    "ModuleNotFoundError",
    "IndentationError",
    "TabError",
)

# HTTP/application-layer signals that point at the app itself.
_FRONTEND_ERROR_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"HTTP\s*5\d{2}", re.IGNORECASE),
    re.compile(r"status\s*5\d{2}", re.IGNORECASE),
    re.compile(r"net::ERR_(?:CONNECTION|NAME|INTERNET|TUNNEL)", re.IGNORECASE),
    re.compile(r"Cannot (?:GET|POST) ", re.IGNORECASE),
    re.compile(r"Application Error", re.IGNORECASE),
]


# Pull an element id out of a Playwright error message. The resolver
# emits messages like
#   "no selector resolved: - role_name='button' ... ('open_stewie_assistant')"
# and the generated POM often surfaces the id directly as
#   "... locate('<id>') ..." in the traceback frame.
_LOCATE_ID_RE = re.compile(r"locate\(\s*['\"]([a-z][a-z0-9_]*)['\"]", re.IGNORECASE)
_SELECTORS_ID_RE = re.compile(
    r"""element_id\s*['"]([a-z][a-z0-9_]*)['"]""", re.IGNORECASE
)


def _extract_referenced_element_id(error_message: str, raw_traceback: str) -> str:
    """Best-effort: pull the element id the failing step was targeting.

    Looks in the error message first (short), then the raw traceback.
    Returns ``""`` when nothing can be extracted.
    """
    for src in (error_message, raw_traceback):
        if not src:
            continue
        m = _LOCATE_ID_RE.search(src)
        if m:
            return m.group(1)
        m = _SELECTORS_ID_RE.search(src)
        if m:
            return m.group(1)
    return ""


def classify_origin(
    *,
    error_message: str,
    error_type: str,
    raw_traceback: str,
    failure_class: str,
    element_id: str,
    known_element_ids: set[str] | None,
) -> str:
    """Decide whether this failure is a script bug or a frontend bug.

    Decision tree (first match wins):

    1. **Python exception types** (``AttributeError``, ``NameError``,
       ``SyntaxError``, ``KeyError``, ``ImportError`` and friends) →
       **script**. These come from the generated test code itself —
       the app can't produce them.
    2. **Explicit frontend signals** (``HTTP 5xx``, ``net::ERR_*``,
       ``"Application Error"``, …) → **frontend**.
    3. **Locator-not-found / not-attached / not-visible**: the
       decision hinges on whether ``element_id`` is in the current
       extraction catalog ``known_element_ids``.
         * id **present** in the catalog → the extractor saw it, the
           running app doesn't → **frontend** (UI changed under us).
         * id **absent** from the catalog → the LLM/planner
           invented or mis-picked it → **script**.
         * no id extractable → **ambiguous**.
    4. **wrong_kind** (``.fill()`` on a button etc.) → **script** —
       the generated code used the wrong Playwright primitive.
    5. **disabled** → **ambiguous**. Could be a missing prerequisite
       click (script) or a genuine app state bug (frontend).
       Default to ambiguous so the LLM can still try.
    6. **intercepted** → **ambiguous** (modal overlay could be either).
    7. **timeout** with no id cue → **ambiguous**.
    8. Otherwise → **ambiguous**.
    """
    et = (error_type or "").split(".")[-1]
    if et in _SCRIPT_ERROR_TYPES or any(
        name in (raw_traceback or "") for name in _SCRIPT_ERROR_TYPES
    ):
        return "script"

    combined = f"{error_message}\n{raw_traceback}"
    if any(p.search(combined) for p in _FRONTEND_ERROR_PATTERNS):
        return "frontend"

    if failure_class in {"locator_not_found", "not_attached", "not_visible"}:
        if not element_id:
            return "ambiguous"
        if known_element_ids is None:
            return "ambiguous"
        return "frontend" if element_id in known_element_ids else "script"

    if failure_class == "wrong_kind":
        return "script"

    return "ambiguous"


# ---------------------------------------------------------------------------
# Subprocess driver
# ---------------------------------------------------------------------------


def run_pytest_capture(
    *,
    test_paths: list[Path],
    junit_path: Path,
    extra_args: list[str] | None = None,
    element_ids_by_slug: dict[str, set[str]] | None = None,
) -> list[PytestFailure]:
    """Run ``pytest`` and return parsed failures.

    The actual test outcome is *ignored* — heal cares only about
    failures, and a green run produces an empty list.

    ``element_ids_by_slug`` is forwarded to ``parse_junit_xml`` so the
    origin classifier can cross-check error-cited element ids against
    each slug's extraction catalog.
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
    return parse_junit_xml(junit_path, element_ids_by_slug=element_ids_by_slug)


# ---------------------------------------------------------------------------
# JUnit XML parsing
# ---------------------------------------------------------------------------


# Match a renderer-shaped step function name in a traceback line.
# Two shapes pytest produces:
#   * `tests/steps/test_login.py:28: in _i_click_sign_in_with_microsoft`
#   * `_i_click_sign_in_with_microsoft(login_page)`
# So we accept either `in _name` or `_name(`.
_STEP_FN_RE = re.compile(r"\bin\s+(_[a-z][a-z0-9_]*)\b|\b(_[a-z][a-z0-9_]*)\(")


def _step_function_from_traceback(text: str) -> str:
    """Walk the traceback for the deepest call into a step function.

    Step functions always start with ``_`` and live in
    ``tests/steps/test_*.py``. The deepest match in the trace is the
    one that actually crashed before delegating to a POM.
    """
    candidates: list[str] = []
    for line in text.splitlines():
        for m in _STEP_FN_RE.finditer(line):
            candidates.append(m.group(1) or m.group(2))
    return candidates[-1] if candidates else ""


def _abs_test_file(classname: str, name: str, base: Path) -> Path:
    """Reconstruct an absolute file path from a JUnit `classname`."""
    # pytest classname looks like: `tests.steps.test_login`
    parts = classname.split(".")
    return (base / Path(*parts[:-1]) / f"{parts[-1]}.py").resolve()


def _slug_from_classname(classname: str) -> str:
    """`tests.steps.test_catalog` → `catalog`. Empty when the shape
    doesn't match (e.g. auth_setup tests)."""
    tail = classname.rsplit(".", 1)[-1]
    return tail[len("test_"):] if tail.startswith("test_") else ""


def parse_junit_xml(
    path: Path,
    *,
    base: Path | None = None,
    element_ids_by_slug: dict[str, set[str]] | None = None,
) -> list[PytestFailure]:
    """Parse a JUnit XML report into ``PytestFailure`` records.

    ``element_ids_by_slug`` maps each slug (e.g. ``"catalog"``) to the
    set of element ids present in that slug's current extraction
    catalog. Supplied by callers that want origin classification —
    when absent, every failure gets ``failure_origin="ambiguous"``.
    """
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
        step_fn = _step_function_from_traceback(body)
        slug = _slug_from_classname(classname)
        known_ids = (element_ids_by_slug or {}).get(slug)
        ref_id = _extract_referenced_element_id(first_line, body)
        fclass = classify(body or msg or first_line)
        origin = classify_origin(
            error_message=first_line,
            error_type=err_type,
            raw_traceback=body,
            failure_class=fclass,
            element_id=ref_id,
            known_element_ids=known_ids,
        )
        out.append(
            PytestFailure(
                test_id=f"{classname}::{name}",
                test_file=_abs_test_file(classname, name, base),
                step_function=step_fn,
                error_type=err_type,
                error_message=first_line,
                failure_class=fclass,
                failure_origin=origin,
                referenced_element_id=ref_id,
                raw_traceback=body,
            )
        )
    return out


# ---------------------------------------------------------------------------
# Helpers exposed for tests
# ---------------------------------------------------------------------------


def has_pytest() -> bool:
    return shutil.which("pytest") is not None or shutil.which(sys.executable) is not None
