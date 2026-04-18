"""Tests for per-invocation log file behaviour in autocoder.logger.

The logger must:

* open a fresh timestamped file inside the configured directory on
  the first ``init`` call within a process,
* be idempotent on subsequent ``init`` calls (the orchestrator and
  CLI both call init — the second call must NOT open a second file),
* avoid filename collisions when two invocations land in the same
  second,
* still accept an explicit file path (legacy behaviour).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def fresh_logger():
    """Reload the logger module so each test starts with no open file."""
    import autocoder.logger as logger_mod

    importlib.reload(logger_mod)
    yield logger_mod
    # Clean up file handle for the next test.
    if logger_mod._file_handle is not None:  # type: ignore[attr-defined]
        logger_mod._file_handle.close()  # type: ignore[attr-defined]
    importlib.reload(logger_mod)


# ---------------------------------------------------------------------------
# Directory mode — fresh per-invocation file
# ---------------------------------------------------------------------------


def test_init_with_directory_creates_timestamped_file(fresh_logger, tmp_path: Path) -> None:
    fresh_logger.init(tmp_path / "logs", level="info", command="generate")
    fresh_logger.info("event_one", k=1)

    files = list((tmp_path / "logs").glob("*.log"))
    assert len(files) == 1
    name = files[0].name
    assert name.endswith("-generate.log")
    # Timestamp segment shape: YYYYMMDD-HHMMSS
    ts = name.rsplit("-generate.log", 1)[0]
    assert len(ts) == 15 and ts[8] == "-", f"unexpected timestamp shape: {name!r}"
    assert "event_one" in files[0].read_text(encoding="utf-8")


def test_init_without_command_uses_run(fresh_logger, tmp_path: Path) -> None:
    fresh_logger.init(tmp_path / "logs", level="info")
    files = list((tmp_path / "logs").glob("*.log"))
    assert len(files) == 1
    assert files[0].name.endswith("-run.log")


def test_init_is_idempotent_within_process(fresh_logger, tmp_path: Path) -> None:
    """The CLI calls init first, then the orchestrator/heal also calls
    init. The second call must NOT open a second file."""
    fresh_logger.init(tmp_path / "logs", level="info", command="generate")
    fresh_logger.init(tmp_path / "logs", level="debug")  # second call

    files = list((tmp_path / "logs").glob("*.log"))
    assert len(files) == 1, f"expected 1 file, got {[f.name for f in files]}"
    # Level should still be updated.
    assert fresh_logger._min_level == 0  # debug


def test_init_collision_gets_numeric_suffix(fresh_logger, tmp_path: Path) -> None:
    """If the timestamped filename already exists (rare — two
    invocations in the same second), the next file gets a -2 suffix."""
    logs = tmp_path / "logs"
    logs.mkdir()

    # Pre-create what the natural filename would be by capturing the
    # current timestamp the same way the logger does, then poking a file.
    import time
    ts = time.strftime("%Y%m%d-%H%M%S")
    (logs / f"{ts}-generate.log").write_text("existing", encoding="utf-8")
    (logs / f"{ts}-generate-2.log").write_text("also existing", encoding="utf-8")

    fresh_logger.init(logs, level="info", command="generate")
    active = fresh_logger.active_log_path()
    assert active is not None
    assert active.name == f"{ts}-generate-3.log"


def test_init_with_explicit_file_path_uses_that_file(fresh_logger, tmp_path: Path) -> None:
    """Legacy callers that pass a real file path keep their existing
    append-to-one-file semantics."""
    target = tmp_path / "custom.log"
    fresh_logger.init(target, level="info")
    fresh_logger.info("hello", n=1)
    assert fresh_logger.active_log_path() == target
    assert "hello" in target.read_text(encoding="utf-8")


def test_init_without_path_is_console_only(fresh_logger) -> None:
    fresh_logger.init(None, level="info")
    assert fresh_logger.active_log_path() is None
    fresh_logger.info("only_console")  # must not raise


# ---------------------------------------------------------------------------
# Sanity: written events are JSON
# ---------------------------------------------------------------------------


def test_written_lines_are_json(fresh_logger, tmp_path: Path) -> None:
    import json

    fresh_logger.init(tmp_path / "logs", level="info", command="extend")
    fresh_logger.info("one", k=1)
    fresh_logger.warn("two", reason="x")

    log = next((tmp_path / "logs").glob("*.log"))
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
    events = [d["event"] for d in lines]
    assert events == ["one", "two"]
