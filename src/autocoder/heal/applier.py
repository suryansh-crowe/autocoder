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


def _body_line_range(source: str, function_name: str) -> tuple[int, int] | None:
    """Return 1-based (start, end) inclusive line numbers of the body
    of ``function_name``. End is the last line of the last statement
    in the body. ``None`` when the function can't be located.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            if not node.body:
                return None
            start = node.body[0].lineno
            last = node.body[-1]
            end = getattr(last, "end_lineno", last.lineno) or last.lineno
            return start, end
    return None


def apply_heal(stub: StubInfo, new_body: str) -> str:
    """Return the file's new contents with `stub` replaced by `new_body`.

    Replaces the **entire current body** of ``stub.function_name`` —
    from the first body line to the last — not just ``stub.body_lineno``.
    This matters whenever a prior heal pass already expanded the body
    into multiple lines; without a range-based replace the new body
    gets inserted in front of the old one and the whole function
    accumulates duplicate statements across runs.

    Raises ``ValueError`` if the resulting source no longer parses —
    in that case the caller must keep the original file untouched.
    """
    original = stub.file_path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)

    # Find the CURRENT range of the function's body. ``stub.body_lineno``
    # is the line the scanner saw at scan time; if the file has since
    # been rewritten (e.g. by a prior heal pass in this session) the
    # range may have shifted, so we re-resolve from the current source.
    rng = _body_line_range(original, stub.function_name)
    if rng is None:
        # Fall back to single-line replacement on the original line.
        start_idx = stub.body_lineno - 1
        if start_idx < 0 or start_idx >= len(lines):
            raise ValueError(
                f"body line {stub.body_lineno} out of range for {stub.file_path}"
            )
        end_idx = start_idx
    else:
        start_idx = rng[0] - 1
        end_idx = rng[1] - 1

    if start_idx < 0 or end_idx >= len(lines) or end_idx < start_idx:
        raise ValueError(
            f"body range {start_idx + 1}-{end_idx + 1} out of range "
            f"for {stub.file_path}"
        )

    indent = _indent_for(start_idx + 1, lines)
    eol = "\r\n" if lines[start_idx].endswith("\r\n") else "\n"

    # ``new_body`` may itself be multi-line if ast.unparse produced one.
    # Indent each line consistently.
    new_lines = [indent + ln for ln in new_body.splitlines() if ln]
    if not new_lines:
        new_lines = [indent + "pass"]
    replacement = eol.join(new_lines) + eol

    rebuilt = (
        "".join(lines[:start_idx])
        + replacement
        + "".join(lines[end_idx + 1 :])
    )

    # Sanity check — never write a file that won't parse.
    try:
        ast.parse(rebuilt, filename=str(stub.file_path))
    except SyntaxError as exc:
        raise ValueError(
            f"applied body broke parse of {stub.file_path}: {exc!s}"
        ) from exc

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
