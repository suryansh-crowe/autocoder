"""AST scanner — locate Playwright test function stubs for healing.

The scanner walks ``tests/playwright/test_<slug>.py`` files and
returns every top-level ``test_*`` function whose body is exactly the
renderer-shaped stub:

    def test_<name>(<fixture>: <Class>) -> None:
        \"\"\"...\"\"\"
        raise NotImplementedError("Implement step: <text>")

Multi-statement bodies (healed tests, hand-edited tests) are left
alone — we never overwrite human or previously-healed work through
the stub-scan path. The failure-driven heal path uses
:func:`find_function_in_file` instead, which matches by name and
operates on whatever body is currently there.

Each ``StubInfo`` carries enough context for the runner to call the
LLM without re-parsing the file.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


_STUB_MESSAGE_PREFIXES = ("Implement step:", "Implement test:")


@dataclass(frozen=True)
class StubInfo:
    file_path: Path
    function_name: str
    body_start_lineno: int     # first line of the body (after the def / docstring)
    body_end_lineno: int       # last line of the body (inclusive)
    scenario_title: str
    fixture_name: str
    fixture_class: str
    pom_module: str            # e.g. "login_page"

    @property
    def slug(self) -> str:
        # ``tests/playwright/test_<slug>.py``
        stem = self.file_path.stem
        return stem.removeprefix("test_") or stem


def _extract_stub_text(call_node: ast.Call) -> str | None:
    """Return the trailing text from ``NotImplementedError('Implement ...: X')``."""
    if not call_node.args or not isinstance(call_node.args[0], ast.Constant):
        return None
    raw = call_node.args[0].value
    if not isinstance(raw, str):
        return None
    for prefix in _STUB_MESSAGE_PREFIXES:
        if raw.startswith(prefix):
            return raw[len(prefix):].strip()
    return None


def _is_stub_body(body: list[ast.stmt]) -> ast.Call | None:
    """Return the NotImplementedError call if ``body`` is *exactly* the stub shape.

    Accepts both:
      * body = [raise NotImplementedError(...)]
      * body = [docstring, raise NotImplementedError(...)]
    so scenarios whose only statement is the stub are heal-eligible.
    """
    stmts = list(body)
    if stmts and isinstance(stmts[0], ast.Expr) and isinstance(stmts[0].value, ast.Constant) and isinstance(stmts[0].value.value, str):
        stmts = stmts[1:]
    if len(stmts) != 1:
        return None
    stmt = stmts[0]
    if not isinstance(stmt, ast.Raise) or stmt.exc is None:
        return None
    exc = stmt.exc
    if not isinstance(exc, ast.Call):
        return None
    if not isinstance(exc.func, ast.Name) or exc.func.id != "NotImplementedError":
        return None
    return exc


def _fixture_param(args: ast.arguments) -> tuple[str, str] | None:
    """Pull (fixture_name, fixture_class) out of a Playwright test function's args.

    The renderer always declares the POM fixture as the first
    positional arg with an annotation: ``def test_x(pom: LoginPage)``.
    """
    if not args.args:
        return None
    first = args.args[0]
    fixture_name = first.arg
    if isinstance(first.annotation, ast.Name):
        return fixture_name, first.annotation.id
    return None


def _pom_module_from_imports(tree: ast.Module) -> str | None:
    """Return the module name imported as ``from tests.pages.X import Y``."""
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module and node.module.startswith("tests.pages."):
            return node.module.split(".")[-1]
    return None


def _docstring(node: ast.FunctionDef) -> str:
    if (
        node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    ):
        return node.body[0].value.value.strip()
    return ""


def _body_span(node: ast.FunctionDef) -> tuple[int, int]:
    """Return ``(first_body_lineno, last_body_end_lineno)`` for ``node``.

    Skips the docstring if present so the heal applier replaces just
    the executable body.
    """
    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if not body:
        # Empty body — defensively fall back to the function header line.
        return node.lineno + 1, node.lineno + 1
    start = body[0].lineno
    end = body[-1].end_lineno or body[-1].lineno
    return start, end


def find_stubs_in_file(path: Path) -> list[StubInfo]:
    """Return every renderer-shaped stub in ``path``."""
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []
    pom_module = _pom_module_from_imports(tree) or ""

    out: list[StubInfo] = []
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("test_"):
            continue
        stub_call = _is_stub_body(node.body)
        if stub_call is None:
            continue
        fx = _fixture_param(node.args)
        if fx is None:
            continue
        body_start, body_end = _body_span(node)
        out.append(
            StubInfo(
                file_path=path,
                function_name=node.name,
                body_start_lineno=body_start,
                body_end_lineno=body_end,
                scenario_title=_docstring(node) or node.name,
                fixture_name=fx[0],
                fixture_class=fx[1],
                pom_module=pom_module,
            )
        )
    return out


def find_function_in_file(path: Path, function_name: str) -> StubInfo | None:
    """Locate a test function by name, whether or not its body is a stub.

    Used by the failure-driven heal flow to target real code (not just
    untouched stubs) so failing tests can be rewritten.
    """
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, SyntaxError):
        return None
    pom_module = _pom_module_from_imports(tree) or ""
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name != function_name:
            continue
        fx = _fixture_param(node.args)
        if fx is None:
            return None
        body_start, body_end = _body_span(node)
        return StubInfo(
            file_path=path,
            function_name=node.name,
            body_start_lineno=body_start,
            body_end_lineno=body_end,
            scenario_title=_docstring(node) or node.name,
            fixture_name=fx[0],
            fixture_class=fx[1],
            pom_module=pom_module,
        )
    return None


def find_stubs_in_dir(dir_path: Path, *, slug: str | None = None) -> list[StubInfo]:
    """Walk ``dir_path`` for ``test_*.py`` files and collect every stub.

    When ``slug`` is supplied, restrict to ``test_<slug>.py`` only.
    """
    if not dir_path.is_dir():
        return []
    pattern = f"test_{slug}.py" if slug else "test_*.py"
    out: list[StubInfo] = []
    for f in sorted(dir_path.glob(pattern)):
        out.extend(find_stubs_in_file(f))
    return out


def filter_unique(stubs: Iterable[StubInfo]) -> list[StubInfo]:
    """Drop stubs whose ``(file, function_name)`` we have seen already."""
    seen: set[tuple[Path, str]] = set()
    out: list[StubInfo] = []
    for s in stubs:
        key = (s.file_path, s.function_name)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out
