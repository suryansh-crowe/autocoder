"""Validate a single-statement body suggested by the LLM.

Hard rules (enforced via AST inspection — never via regex on the body):

* The body must parse with ``ast.parse``.
* The body must contain exactly one statement.
* That statement must be one of: ``ast.Expr`` (function-call /
  ``expect(...)`` chain), ``ast.Pass``, ``ast.Assert``, ``ast.Raise``.
* No ``import``, ``def``, ``class``, ``with``, ``for``, ``while``,
  ``try``, lambda, or comprehension.
* Any attribute reference of shape ``<fixture>.<name>`` whose object
  is the POM fixture must reference a real method on the POM
  (``page`` is allowed because it is the underlying Playwright Page;
  ``locate`` is allowed because it lives on ``BasePage``).

The function returns ``(cleaned_body, errors)``. When ``errors`` is
non-empty the body is unsafe and must be discarded.
"""

from __future__ import annotations

import ast


_ALLOWED_STMT_TYPES: tuple[type, ...] = (ast.Expr, ast.Pass, ast.Assert, ast.Raise)
_FORBIDDEN_NODE_TYPES: tuple[type, ...] = (
    ast.Import,
    ast.ImportFrom,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.With,
    ast.AsyncWith,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Try,
    ast.Lambda,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)
# These are always safe to reference on the POM fixture even though
# they aren't in `pom_methods` — they live on the BasePage / Playwright
# Page surface that every generated POM inherits.
_BUILTIN_FIXTURE_ATTRS = frozenset({"navigate", "locate", "page", "goto", "URL", "SELECTORS"})


def _walk_calls(node: ast.AST) -> list[ast.Call]:
    return [n for n in ast.walk(node) if isinstance(n, ast.Call)]


def _illegal_constructs(node: ast.AST) -> list[str]:
    bad: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, _FORBIDDEN_NODE_TYPES):
            bad.append(type(child).__name__)
    return bad


def validate_body(
    body_text: str,
    *,
    fixture_name: str,
    pom_method_names: set[str],
    element_ids: set[str] | None = None,
    max_statements: int = 1,
) -> tuple[str, list[str]]:
    """Return (cleaned_body, errors). Empty errors → body is safe.

    ``max_statements`` controls how many top-level statements are
    permitted. Stub heal (single statement is safe and unambiguous)
    uses the default of 1; failure-driven heal uses 5 because
    real-world fixes often need a prerequisite call before retrying
    (`pom.check_box(); pom.click_submit()`).

    ``element_ids`` is the set of keys present in the POM's
    ``SELECTORS`` dict. When provided, any string literal passed to
    ``<fixture>.locate(...)`` / ``<fixture>.click(...)`` /
    ``<fixture>.check(...)`` / ``<fixture>.fill(...)`` /
    ``<fixture>.select(...)`` is rejected if it is not a known key.
    This guards against the LLM inventing element ids like
    ``"title"`` / ``"response"`` / ``"validation-error-message"``
    that would explode at runtime with ``KeyError``.
    """
    text = (body_text or "").strip()
    if not text:
        return "", ["empty body"]

    try:
        tree = ast.parse(text, mode="exec")
    except SyntaxError as exc:
        return "", [f"syntax error: {exc.msg}"]

    if not tree.body:
        return "", ["empty body"]
    if len(tree.body) > max_statements:
        return "", [f"too many statements: got {len(tree.body)}, max {max_statements}"]

    for stmt in tree.body:
        if not isinstance(stmt, _ALLOWED_STMT_TYPES):
            return "", [f"forbidden top-level node {type(stmt).__name__}"]
        illegal = _illegal_constructs(stmt)
        if illegal:
            return "", [f"forbidden construct(s): {', '.join(sorted(set(illegal)))}"]

    # Any `<fixture>.<method>(...)` call where the object is the POM
    # fixture must reference a real method, `navigate`, `locate`, or
    # `page`. Other fixtures (e.g. `expect(...)`) are fine — they're
    # global names from the import header.
    #
    # We also reject string arguments to ``locate``/``click``/``check``/
    # ``fill``/``select`` that do not appear in the POM's SELECTORS
    # catalogue, because those crash at runtime with KeyError.
    errors: list[str] = []
    element_lookups = {"locate", "click", "check", "fill", "select"}
    for stmt in tree.body:
        for call in _walk_calls(stmt):
            target = call.func
            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                obj = target.value.id
                attr = target.attr
                if obj == fixture_name and attr not in pom_method_names and attr not in _BUILTIN_FIXTURE_ATTRS:
                    errors.append(
                        f"unknown method {fixture_name}.{attr}() — "
                        f"must be in pom_methods or a BasePage attribute"
                    )
                    continue
                if (
                    element_ids is not None
                    and obj == fixture_name
                    and attr in element_lookups
                    and call.args
                ):
                    first = call.args[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        if first.value not in element_ids:
                            errors.append(
                                f"unknown element_id {first.value!r} passed to "
                                f"{fixture_name}.{attr}(...) — must be one of "
                                f"the keys in SELECTORS ({sorted(element_ids)[:6]}"
                                f"{'...' if len(element_ids) > 6 else ''})"
                            )

    if errors:
        return "", errors
    # Re-emit so weird whitespace gets normalised but indentation stays
    # at column zero; the applier will indent on insertion.
    cleaned = ast.unparse(tree).strip()
    return cleaned, []
