"""Replace a test function body with validated statements.

The heal stage operates at the **test-function level**: the applier
swaps every line between ``body_start_lineno`` and ``body_end_lineno``
(inclusive) with the new list of statements, preserving the indent
of the first body line and the file's existing line-ending style.

The rest of the file (docstring, decorators, sibling tests, imports)
is untouched. A post-replace ``ast.parse`` guards against any change
that breaks the file — if it does, :class:`ValueError` is raised and
the caller keeps the original file.
"""

from __future__ import annotations

import ast
from pathlib import Path

from autocoder.heal.scanner import StubInfo


def _indent_for(lineno: int, lines: list[str]) -> str:
    """Return the leading whitespace of ``lineno`` (1-based)."""
    if 1 <= lineno <= len(lines):
        line = lines[lineno - 1]
        return line[: len(line) - len(line.lstrip())]
    return "    "


def _detect_eol(line: str) -> str:
    return "\r\n" if line.endswith("\r\n") else "\n"


def apply_heal(stub: StubInfo, new_body: str) -> str:
    """Return the file's new contents with the test body replaced.

    ``new_body`` may be a single statement or multiple statements
    separated by newlines (``\\n``). Each non-empty line is re-indented
    to the original body's indentation; empty lines are dropped so the
    output is tidy.

    Raises :class:`ValueError` when the resulting source no longer
    parses — the caller must keep the original file untouched in that
    case.
    """
    original = stub.file_path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    start_idx = stub.body_start_lineno - 1
    end_idx = stub.body_end_lineno - 1
    if start_idx < 0 or end_idx >= len(lines) or end_idx < start_idx:
        raise ValueError(
            f"body span {stub.body_start_lineno}-{stub.body_end_lineno} "
            f"out of range for {stub.file_path}"
        )

    indent = _indent_for(stub.body_start_lineno, lines)
    eol = _detect_eol(lines[start_idx])

    stmts = [s for s in (ln.rstrip() for ln in new_body.splitlines()) if s]
    if not stmts:
        stmts = ["pass"]
    replacement = "".join(indent + stmt + eol for stmt in stmts)

    rebuilt = "".join(lines[:start_idx]) + replacement + "".join(lines[end_idx + 1 :])

    try:
        ast.parse(rebuilt, filename=str(stub.file_path))
    except SyntaxError as exc:
        raise ValueError(f"applied body broke parse of {stub.file_path}: {exc!s}") from exc

    return rebuilt


def write_if_changed(stub: StubInfo, new_text: str) -> bool:
    """Write ``new_text`` to ``stub.file_path`` only if it differs."""
    current = stub.file_path.read_text(encoding="utf-8")
    if current == new_text:
        return False
    stub.file_path.write_text(new_text, encoding="utf-8")
    return True


def write_path_if_changed(path: Path, new_text: str) -> bool:
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if current == new_text:
        return False
    path.write_text(new_text, encoding="utf-8")
    return True
