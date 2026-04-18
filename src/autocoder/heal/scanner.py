"""AST scanner — locate renderer-produced NotImplementedError stubs.

The scanner only flags bodies that match the **exact shape the
renderer emits**:

    def _<slug>(<fixture>: <Class>) -> None:
        raise NotImplementedError("Implement step: <text>")

Anything else (a hand-edited body, a multi-statement body, a stub the
user already replaced) is left alone. That preserves user edits across
heal runs.

Each ``StubInfo`` carries enough context for the runner to call the
LLM without re-parsing the file.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


_STUB_MESSAGE_PREFIX = "Implement step:"


@dataclass(frozen=True)
class StubInfo:
    file_path: Path
    function_name: str
    body_lineno: int       # line of the `raise NotImplementedError(...)`
    body_col_offset: int
    step_text: str
    keywords: tuple[str, ...]   # one or more of Given/When/Then
    fixture_name: str
    fixture_class: str
    pom_module: str        # e.g. "login_page" — used to resolve manifest paths

    @property
    def slug(self) -> str:
        # `tests/steps/test_<slug>.py`
        stem = self.file_path.stem
        return stem.removeprefix("test_") or stem


def _extract_step_text(call_node: ast.Call) -> str | None:
    """Return the step text from `NotImplementedError("Implement step: X")`."""
    if not call_node.args or not isinstance(call_node.args[0], ast.Constant):
        return None
    raw = call_node.args[0].value
    if not isinstance(raw, str) or not raw.startswith(_STUB_MESSAGE_PREFIX):
        return None
    return raw[len(_STUB_MESSAGE_PREFIX):].strip().split(" (POM method ")[0]


def _is_stub_body(body: list[ast.stmt]) -> ast.Call | None:
    """Return the NotImplementedError call if `body` is *exactly* the stub
    shape the renderer produces, else None. A single-statement body is
    required so we never overwrite hand-edited multi-line bodies.
    """
    if len(body) != 1:
        return None
    stmt = body[0]
    if not isinstance(stmt, ast.Raise) or stmt.exc is None:
        return None
    exc = stmt.exc
    if not isinstance(exc, ast.Call):
        return None
    if not isinstance(exc.func, ast.Name) or exc.func.id != "NotImplementedError":
        return None
    return exc


def _decorator_keywords(decorator_list: list[ast.expr]) -> tuple[str, ...]:
    """Return the set of pytest-bdd keywords stacked on a step function."""
    seen: list[str] = []
    for dec in decorator_list:
        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name):
            kw = dec.func.id
            if kw in {"given", "when", "then"} and kw not in seen:
                seen.append(kw)
    # Capitalise back to the Gherkin form for clarity in prompts/logs.
    return tuple(k.capitalize() for k in seen)


def _fixture_param(args: ast.arguments) -> tuple[str, str] | None:
    """Pull (fixture_name, fixture_class) out of a step function's args.

    The renderer always declares the POM fixture as the first
    positional arg with an annotation: ``def _x(login_page: LoginPage)``.
    """
    if not args.args:
        return None
    first = args.args[0]
    fixture_name = first.arg
    if isinstance(first.annotation, ast.Name):
        return fixture_name, first.annotation.id
    return None


def _pom_module_from_imports(tree: ast.Module) -> str | None:
    """Return the module name imported as `from tests.pages.X import Y`."""
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.module and node.module.startswith("tests.pages."):
            return node.module.split(".")[-1]
    return None


def find_stubs_in_file(path: Path) -> list[StubInfo]:
    """Return every renderer-shaped stub in `path`."""
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
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith("_"):
            continue
        stub_call = _is_stub_body(node.body)
        if stub_call is None:
            continue
        step_text = _extract_step_text(stub_call)
        if not step_text:
            continue
        fx = _fixture_param(node.args)
        if fx is None:
            continue
        keywords = _decorator_keywords(node.decorator_list)
        if not keywords:
            continue
        out.append(
            StubInfo(
                file_path=path,
                function_name=node.name,
                body_lineno=node.body[0].lineno,
                body_col_offset=node.body[0].col_offset,
                step_text=step_text,
                keywords=keywords,
                fixture_name=fx[0],
                fixture_class=fx[1],
                pom_module=pom_module,
            )
        )
    return out


def find_stubs_in_dir(dir_path: Path, *, slug: str | None = None) -> list[StubInfo]:
    """Walk `dir_path` for `test_*.py` files and collect every stub.

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
    """Drop stubs whose `(file, function_name)` we have seen already."""
    seen: set[tuple[Path, str]] = set()
    out: list[StubInfo] = []
    for s in stubs:
        key = (s.file_path, s.function_name)
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out
