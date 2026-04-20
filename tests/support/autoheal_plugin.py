"""Pytest plugin: heal failing step bodies live during a pytest run.

Activation
----------
Set ``AUTOCODER_AUTOHEAL=true`` (case-insensitive) in the environment
(or your ``.env``, which ``conftest.py`` already loads). The plugin
is otherwise a no-op — regular pytest runs are unaffected unless the
flag is on.

What it does
------------
1. Watches every test in the session. Whenever a test fails at the
   **call** phase (not at setup/teardown), captures the test id and
   the pytest-rendered traceback.
2. At end-of-session, if any failures were captured:
     a. Writes a synthetic JUnit XML to
        ``manifest/heals/autoheal-session.xml`` that lists exactly
        those failures (same schema as ``pytest --junit-xml``).
     b. Invokes :func:`autocoder.heal.heal_steps` with
        ``from_pytest=True`` pointing at that XML. The heal engine
        parses the failure, pulls the current step body, the cached
        POM plan, and the extraction's element catalog, then asks the
        configured LLM for a revised body. Every suggestion is AST-
        validated (method names must exist in the POM; element ids
        must exist in ``SELECTORS``) before the step file is written.
     c. Prints a summary line: how many step bodies were patched.
3. Optionally, if ``AUTOCODER_AUTOHEAL_RERUN=true``, spawns
   ``pytest --last-failed`` as a subprocess so the user sees the
   healed tests pass without typing anything.

Why not heal mid-test?
----------------------
Pytest imports step modules once at collection time. Patching the
source file mid-run requires ``importlib.reload``, fixture-cache
invalidation, and re-resolving Item.obj — each of which breaks
subtly across pytest versions. The post-session design side-steps
all of that: the original run reports the true failure surface, the
heal engine operates on a fixed snapshot, and the optional rerun
subprocess verifies the fix cleanly in a fresh pytest session.

Enabling
--------
```env
# Heal failing steps after every pytest session.
AUTOCODER_AUTOHEAL=true

# Also auto-run `pytest --last-failed` after the heal completes.
AUTOCODER_AUTOHEAL_RERUN=true
```

Per-run override:

```powershell
AUTOCODER_AUTOHEAL=true pytest tests/playwright
```
"""

from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _enabled() -> bool:
    return _flag("AUTOCODER_AUTOHEAL")


def _rerun_enabled() -> bool:
    return _flag("AUTOCODER_AUTOHEAL_RERUN")


# (nodeid, classname, name, longrepr_text)
_failures: list[tuple[str, str, str, str]] = []


@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_runtest_makereport(item, call):  # noqa: ARG001
    outcome = yield
    if not _enabled():
        return
    report = outcome.get_result()
    if report.when != "call" or not report.failed:
        return
    # pytest rendering of the traceback — same text the user sees,
    # matches what ``run_pytest_capture`` would have written to XML.
    longrepr = str(report.longrepr) if report.longrepr is not None else ""
    # Reconstruct (classname, name) the way pytest --junit-xml would.
    nodeid = item.nodeid
    if "::" in nodeid:
        path_part, test_part = nodeid.rsplit("::", 1)
    else:
        path_part, test_part = nodeid, ""
    classname = path_part.replace("/", ".").replace("\\", ".").rstrip(".py").rstrip(".")
    if classname.endswith(".py"):
        classname = classname[:-3]
    _failures.append((nodeid, classname, test_part, longrepr))


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    if not _enabled() or not _failures:
        return

    # Build a JUnit-XML-shaped payload the heal engine already knows
    # how to parse. We could pass structured PytestFailure objects,
    # but going through XML keeps the plugin decoupled from heal's
    # internals and reuses the same parser path the CLI uses.
    try:
        from autocoder import logger as _logger
        from autocoder.config import load_settings
        from autocoder.heal import HealOptions, heal_steps
    except Exception as exc:  # noqa: BLE001
        print(f"[autoheal] could not import autocoder: {exc!s}", file=sys.stderr)
        return

    settings = load_settings()
    _logger.init(
        settings.paths.logs_dir,
        level=settings.log_level,
        command="autoheal",
    )
    junit_dir = settings.paths.manifest_dir / "heals"
    junit_dir.mkdir(parents=True, exist_ok=True)
    junit_path = junit_dir / "autoheal-session.xml"

    root = ET.Element("testsuite", attrib={"name": "autoheal_session"})
    for _nodeid, classname, name, longrepr in _failures:
        tc = ET.SubElement(
            root, "testcase", attrib={"classname": classname, "name": name}
        )
        failure = ET.SubElement(
            tc,
            "failure",
            attrib={
                "message": (longrepr.splitlines()[0] if longrepr else "failure")[:200],
                "type": "pytest.Failure",
            },
        )
        failure.text = longrepr
    ET.ElementTree(root).write(str(junit_path), encoding="utf-8", xml_declaration=True)

    _logger.info(
        "autoheal_triggered",
        failures=len(_failures),
        junit=str(junit_path),
    )
    print(
        f"\n[autoheal] {len(_failures)} failure(s) detected -- calling heal engine",
        file=sys.stderr,
    )

    try:
        results = heal_steps(
            settings,
            HealOptions(from_pytest=True, junit_path=junit_path),
        )
    except SystemExit:
        # heal_steps may call logger.die which raises SystemExit; don't
        # take pytest down with it.
        print("[autoheal] heal engine exited early (LLM unreachable?)", file=sys.stderr)
        return
    except Exception as exc:  # noqa: BLE001
        print(f"[autoheal] heal engine raised: {exc!s}", file=sys.stderr)
        return

    applied = sum(1 for r in results if r.applied)
    cached = sum(1 for r in results if r.cached)
    print(
        f"[autoheal] {applied}/{len(results)} step(s) patched "
        f"({cached} cached, {len(results) - applied} rejected)",
        file=sys.stderr,
    )

    if not applied:
        print(
            "[autoheal] nothing was patched -- inspect the step file(s) and "
            "rerun with AUTOCODER_AUTOHEAL=false to restore a clean fail.",
            file=sys.stderr,
        )
        return

    if not _rerun_enabled():
        print(
            "[autoheal] tip: set AUTOCODER_AUTOHEAL_RERUN=true to auto-retry, "
            "or rerun manually with `pytest --last-failed`.",
            file=sys.stderr,
        )
        return

    print("[autoheal] re-running failed tests...", file=sys.stderr)
    # Reuse the same interpreter + pytest; pass --last-failed so
    # only the previously-failing tests run. We intentionally do NOT
    # re-enable autoheal on the rerun: one heal pass per session is
    # the contract, otherwise a bad LLM suggestion could loop.
    env = os.environ.copy()
    env["AUTOCODER_AUTOHEAL"] = "false"
    env["AUTOCODER_AUTOHEAL_RERUN"] = "false"
    subprocess.run(
        [sys.executable, "-m", "pytest", "--last-failed"],
        cwd=str(Path.cwd()),
        env=env,
        check=False,
    )
