"""Replace a stub body with the validated suggestion.

We deliberately use line-based replacement (not full AST round-trip)
so that:

* the rest of the file's formatting, decorators, and ordering are
  preserved exactly,
* hand-edited siblings are never touched,
* a failed parse after the swap aborts the change with a rollback.

The ``StubInfo`` carries the ``body_lineno`` of the original
``raise NotImplementedError(...)`` line. Because the renderer always
emits a single-line body indented by 4 spaces, we replace just that
one line.
"""

from __future__ import annotations

import ast
from pathlib import Path

from autocoder.heal.scanner import StubInfo


def _indent_for(lineno: int, lines: list[str]) -> str:
    """Return the leading whitespace of `lineno` (1-based)."""
    if 1 <= lineno <= len(lines):
        line = lines[lineno - 1]
        return line[: len(line) - len(line.lstrip())]
    return "    "


def apply_heal(stub: StubInfo, new_body: str) -> str:
    """Return the file's new contents with `stub` replaced by `new_body`.

    Raises ``ValueError`` if the resulting source no longer parses —
    in that case the caller must keep the original file untouched.
    """
    original = stub.file_path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    line_idx = stub.body_lineno - 1
    if line_idx < 0 or line_idx >= len(lines):
        raise ValueError(f"body line {stub.body_lineno} out of range for {stub.file_path}")

    indent = _indent_for(stub.body_lineno, lines)
    eol = "\r\n" if lines[line_idx].endswith("\r\n") else "\n"

    # `new_body` may itself be multi-line if ast.unparse produced one.
    # Indent each line consistently.
    new_lines = [indent + ln for ln in new_body.splitlines() if ln]
    if not new_lines:
        new_lines = [indent + "pass"]
    replacement = eol.join(new_lines) + eol

    rebuilt = "".join(lines[:line_idx]) + replacement + "".join(lines[line_idx + 1 :])

    # Sanity check — never write a file that won't parse.
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
