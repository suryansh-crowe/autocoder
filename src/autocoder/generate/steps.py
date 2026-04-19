"""Render pytest-bdd step definition files.

Steps are emitted *deterministically* from the FeaturePlan + POMPlan:

* Each unique step **text** becomes one Python function.
* That function is decorated with every Gherkin keyword the text was
  used under across the feature (Given/When/Then). pytest-bdd allows
  stacking decorators on a single function, so we never end up with
  two functions sharing a Python name.
* If the step references a POM method, the body calls
  ``page_object.<method>(*args)`` — and only when the step actually
  supplies values for every parameter the POM method requires.
* If the step does not reference a POM method, we attempt to
  **synthesize** executable Playwright code from the step text:

  * "the user is on the <X> page" → ``page_object.navigate()``
  * "the <X> is visible" / "is displayed" → ``expect(locator).to_be_visible()``
  * "the <X> checkbox is not checked" → ``expect(locator).not_to_be_checked()``
  * "the <X> checkbox is checked" → ``expect(locator).to_be_checked()``
  * "the user clicks the <X>" → ``page_object.<fuzzy-match>()`` if one exists.

  Synthesis uses the ``Element`` catalog the POM was built from so the
  generated calls reference the *same* selector keys the runtime
  resolver knows how to self-heal.

* Only when synthesis fails do we emit ``NotImplementedError`` — and
  the orchestrator counts those occurrences as a quality-gate signal.
"""

from __future__ import annotations

import re
from collections import OrderedDict

from autocoder.models import Element, FeaturePlan, POMPlan, StepRef


_HEADER = '''"""Generated step definitions for {feature_title!r}."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect
from pytest_bdd import given, parsers, scenarios, then, when

from tests.pages.{module_name} import {class_name}

scenarios("{feature_path}")


@pytest.fixture
def {fixture_name}(page: Page) -> {class_name}:
    return {class_name}(page)
'''


_STEP_TPL = '''

{decorators}
def _{slug}({fixture_name}: {class_name}{extra_params}) -> None:
    {body}
'''


# Map Gherkin keywords to pytest-bdd decorators.
# `And` / `But` inherit the previous step's keyword in Gherkin; we
# resolve them at parse time, so by the time they reach _decorator_for
# they should already have been promoted. As a fallback, treat them
# as @given (the most permissive matcher).
_KEYWORD_TO_DECORATOR = {
    "Given": "given",
    "When": "when",
    "Then": "then",
    "And": "given",
    "But": "given",
}


def _slug(text: str, idx: int) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    if not cleaned:
        cleaned = f"step_{idx}"
    return cleaned[:50]


def _matcher(text: str) -> tuple[str, list[str]]:
    """Return a parsers.parse-style matcher and any extracted arg names.

    Quoted segments become {arg0}, {arg1}, ...
    """
    parts: list[str] = []
    args: list[str] = []
    last = 0
    for i, m in enumerate(re.finditer(r'"([^"]+)"', text)):
        parts.append(text[last : m.start()])
        arg = f"arg{i}"
        parts.append(f'"{{{arg}}}"')
        args.append(arg)
        last = m.end()
    parts.append(text[last:])
    pattern = "".join(parts)
    return f"parsers.parse({pattern!r})", args


def _resolve_keywords(steps: list[StepRef]) -> list[StepRef]:
    """Promote ``And`` / ``But`` to the keyword of the previous step.

    Operates in scenario / background order so the promotion is correct
    even when a scenario opens with ``And`` (defensive fallback to
    ``Given`` in that case).
    """
    out: list[StepRef] = []
    last_kw = "Given"
    for s in steps:
        kw = s.keyword
        if kw in {"And", "But"}:
            kw = last_kw
        else:
            last_kw = kw
        if kw == s.keyword:
            out.append(s)
        else:
            out.append(s.model_copy(update={"keyword": kw}))
    return out


# Words that add no information when matching step text to elements /
# POM methods. Keep this list focused on *noise*, not on content.
#
# IMPORTANT: negation words ("not", "without", "never", "doesn't", "does")
# and anchors like "checked"/"disabled" are NOT in this set — they carry
# meaning that the assertion/negation branches below need to read.
_STOPWORDS = {
    "the", "a", "an", "user", "users", "is", "are", "on", "in", "of", "to",
    "page", "screen", "view", "with", "and", "or",
    "taps", "tap", "presses", "press", "selects", "enters",
    "enter", "types", "type", "into", "onto",
    "at", "from", "for", "that", "this", "sees", "see", "seen",
}

_NEGATION_RE = re.compile(
    r"\b(does\s+not|doesn'?t|do\s+not|don'?t|did\s+not|didn'?t|"
    r"will\s+not|won'?t|cannot|can'?t|should\s+not|shouldn'?t|"
    r"must\s+not|mustn'?t|without|never|no\s+longer)\b",
    re.IGNORECASE,
)


def _normalize(text: str) -> list[str]:
    """Lowercase + tokenize step text, dropping common filler words."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [w for w in words if w and w not in _STOPWORDS and len(w) > 1]


def _best_element_match(tokens: list[str], elements: list[Element]) -> Element | None:
    """Heuristic: score each element by token overlap with step text."""
    if not tokens or not elements:
        return None

    def _score(e: Element) -> int:
        pool = " ".join(
            str(x)
            for x in (e.id, e.name or "", e.role or "", e.kind or "")
        ).lower()
        hit = 0
        for t in tokens:
            if t in pool:
                hit += 1
        return hit

    ranked = sorted(elements, key=_score, reverse=True)
    top = ranked[0]
    if _score(top) == 0:
        return None
    return top


def _best_method_match(tokens: list[str], methods: list[str]) -> str | None:
    """Pick a POM method name whose tokens best cover the step tokens."""
    if not tokens or not methods:
        return None

    def _score(name: str) -> int:
        parts = name.lower().split("_")
        return sum(1 for t in tokens if t in parts)

    ranked = sorted(methods, key=_score, reverse=True)
    top = ranked[0]
    if _score(top) < 2:  # demand at least two shared tokens
        return None
    return top


# Each entry: (regex pattern, format template). Template can reference
# ``{locator}`` for a ``page_object.locate(...)`` call against the
# matched element, or ``{fixture}`` / ``{method}`` for fuzzy method
# matches. The first matching entry wins.
#
# The "state" prefix accepts the three ways users phrase an expected
# state: ``is``, ``should be``, ``must be``. "becomes"/"gets" also
# appear in some QA dialects.
_STATE = r"(?:is|should\s+be|must\s+be|becomes|gets)"

_ASSERTION_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(rf"\b{_STATE}\s+not\s+checked\b|\bis\s+unchecked\b", re.IGNORECASE),
     "expect({locator}).not_to_be_checked()"),
    (re.compile(rf"\b{_STATE}\s+checked\b", re.IGNORECASE),
     "expect({locator}).to_be_checked()"),
    (re.compile(rf"\b{_STATE}\s+not\s+visible\b|\b{_STATE}\s+hidden\b", re.IGNORECASE),
     "expect({locator}).not_to_be_visible()"),
    (re.compile(rf"\b{_STATE}\s+(visible|displayed|shown|present)\b", re.IGNORECASE),
     "expect({locator}).to_be_visible()"),
    (re.compile(rf"\b{_STATE}\s+not\s+enabled\b|\b{_STATE}\s+disabled\b", re.IGNORECASE),
     "expect({locator}).to_be_disabled()"),
    (re.compile(rf"\b{_STATE}\s+enabled\b", re.IGNORECASE),
     "expect({locator}).to_be_enabled()"),
)

# Covers "page" suffix and common synonyms: homepage, dashboard,
# landing, home, site, app. Subject can be any of:
# "the user is/am/are on …", "I am/'m on …", "one opens …",
# "navigates to …", "visits …", "goes to …", "lands on …".
_NAV_PATTERN = re.compile(
    r"\b(?:is|am|are|'m|'re)\s+(?:on|at|in)\s+(?:the\s+)?.+\b"
    r"(page|homepage|home\s*page|landing|dashboard|home|site|app)\b"
    r"|\b(?:opens?|navigates?\s+to|visits?|goes?\s+to|lands?\s+on)\s+(?:the\s+)?.+\b"
    r"(page|homepage|home\s*page|landing|dashboard|home|site|app)\b",
    re.IGNORECASE,
)


def _try_synthesize(
    step: StepRef,
    fixture_name: str,
    elements: list[Element],
    pom_method_names: list[str],
) -> str | None:
    """Return a runnable Python body, or ``None`` if we cannot synthesize.

    The caller treats ``None`` as "fall through to NotImplementedError".
    """
    text = step.text or ""
    tokens = _normalize(text)
    is_negated = bool(_NEGATION_RE.search(text))

    # 1. Navigation — "user is on the <X> page" → navigate() on the POM.
    if _NAV_PATTERN.search(text):
        return f"{fixture_name}.navigate()"

    # 2. Direct fuzzy method match — "the user clicks the terms of service checkbox"
    #    → click_terms_of_service_checkbox() if such a method exists.
    #    BUT: if the step is negated ("user does NOT check ..."), calling
    #    the affirmative POM method would be the opposite of what the
    #    scenario asserts. Emit a no-op with an explanatory comment so
    #    the test runs but does nothing, and the intent is documented.
    if step.keyword in ("Given", "When", "And", "But") and not is_negated:
        method = _best_method_match(tokens, pom_method_names)
        if method:
            return f"{fixture_name}.{method}()"

    # 3. Assertion patterns.
    element = _best_element_match(tokens, elements)
    if element is not None:
        locator = f"{fixture_name}.locate({element.id!r})"
        for pattern, tpl in _ASSERTION_PATTERNS:
            if pattern.search(text):
                return tpl.format(locator=locator)
        # Fallback for pure `Then` steps that reference an element but
        # do not state a specific assertion — default to visibility.
        # Only for non-negated Then; "Then X is NOT <something>" must
        # not become "expect visible".
        if step.keyword == "Then" and not is_negated:
            return f"expect({locator}).to_be_visible()"

    # 4. Negated action with no better match — a safe no-op preserves
    #    scenario semantics ("the user does NOT click submit" means
    #    leave things alone) instead of accidentally executing the
    #    positive action via fuzzy matching.
    if is_negated and step.keyword in ("Given", "When", "And", "But"):
        return (
            "pass  # intentional no-op: step text asserts a non-action "
            "(negation detected)"
        )
    return None


def _body(
    step: StepRef,
    extracted_args: list[str],
    fixture_name: str,
    pom_args_by_method: dict[str, list[str]],
    elements: list[Element],
) -> str:
    # Background / navigation hygiene: ``Given I am on the X page`` is
    # almost always a setup step that should actually load the page,
    # not click a nav link the LLM happened to map it to. The
    # feature-plan LLM routinely picks the nearest-sounding ``click_home``
    # / ``click_dashboard`` method here, which then fails at runtime
    # because no URL was loaded yet (the page is still ``about:blank``).
    #
    # Force ``fixture.navigate()`` for Given steps that match the
    # navigation regex, regardless of what the LLM bound. ``navigate``
    # is defined on every generated POM (it inherits from BasePage),
    # so this is always a safe call.
    if step.keyword == "Given" and _NAV_PATTERN.search(step.text or ""):
        return f"{fixture_name}.navigate()"

    if step.pom_method:
        required = pom_args_by_method.get(step.pom_method, [])
        supplied = list(extracted_args) + list(step.args or [])
        if len(supplied) >= len(required):
            return (
                f"{fixture_name}.{step.pom_method}("
                f"{', '.join(supplied[: len(required)] or supplied)})"
            )
        missing = ", ".join(required[len(supplied):])
        return (
            f'raise NotImplementedError('
            f'"Implement step: {step.text} '
            f'(POM method {step.pom_method!r} expects: {missing})")'
        )

    synth = _try_synthesize(
        step, fixture_name, elements, list(pom_args_by_method.keys())
    )
    if synth is not None:
        return synth
    return f'raise NotImplementedError("Implement step: {step.text}")'


def render_steps(
    *,
    feature_title: str,
    feature_path: str,
    feature_plan: FeaturePlan,
    pom_plan: POMPlan,
    pom_module: str,
    elements: list[Element] | None = None,
) -> str:
    """Emit a step-definitions module covering every step in the plan.

    ``elements`` is the extraction element catalog. When provided, the
    renderer can synthesize executable step bodies for common
    assertion/navigation patterns instead of emitting
    ``NotImplementedError``.
    """
    fixture_name = pom_plan.fixture_name
    class_name = pom_plan.class_name
    pom_args_by_method = {m.name: list(m.args or []) for m in pom_plan.methods}
    el_list: list[Element] = list(elements or [])

    parts: list[str] = [
        _HEADER.format(
            feature_title=feature_title,
            module_name=pom_module,
            class_name=class_name,
            feature_path=feature_path,
            fixture_name=fixture_name,
        )
    ]

    # Background runs once before each scenario in Gherkin terms; the
    # keyword inheritance still applies inside background.
    background_resolved = _resolve_keywords(list(feature_plan.background))
    all_steps: list[StepRef] = list(background_resolved)
    for scn in feature_plan.scenarios:
        all_steps.extend(_resolve_keywords(list(scn.steps)))

    # Group by step text. Same text under multiple keywords becomes a
    # single function decorated with each keyword's decorator.
    by_text: "OrderedDict[str, dict]" = OrderedDict()
    for step in all_steps:
        entry = by_text.get(step.text)
        if entry is None:
            entry = {"step": step, "keywords": []}
            by_text[step.text] = entry
        kw = step.keyword
        if kw not in entry["keywords"]:
            entry["keywords"].append(kw)

    for idx, (text, entry) in enumerate(by_text.items()):
        step: StepRef = entry["step"]
        keywords: list[str] = entry["keywords"]

        matcher, extracted_args = _matcher(text)
        decorators = "\n".join(
            f"@{_KEYWORD_TO_DECORATOR.get(kw, 'given')}({matcher})"
            for kw in keywords
        )
        slug = _slug(text, idx)
        body = _body(step, extracted_args, fixture_name, pom_args_by_method, el_list)
        extra_params = "".join(f", {a}: str" for a in extracted_args)

        parts.append(
            _STEP_TPL.format(
                decorators=decorators,
                slug=slug,
                fixture_name=fixture_name,
                class_name=class_name,
                extra_params=extra_params,
                body=body,
            )
        )

    return "".join(parts)
