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
from typing import Iterable
from urllib.parse import urlparse

from autocoder import logger
from autocoder.models import Element, FeaturePlan, POMPlan, StepRef, StepsPlan


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


def _best_element_match(
    tokens: list[str],
    elements: list[Element],
    *,
    forbidden_ids: set[str] | None = None,
) -> Element | None:
    """Heuristic: score each element by token overlap with step text.

    ``forbidden_ids`` lets the caller exclude elements that were
    already acted on earlier in the same scenario — prevents the
    "Then X is visible" fallback from pointing at the same element
    the scenario's When step just clicked (which makes the assertion
    meaningless).
    """
    if not tokens or not elements:
        return None
    forbidden = forbidden_ids or set()

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

    pool = [e for e in elements if e.id not in forbidden]
    if not pool:
        return None
    ranked = sorted(pool, key=_score, reverse=True)
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
    *,
    forbidden_ids: set[str] | None = None,
) -> str | None:
    """Return a runnable Python body, or ``None`` if we cannot synthesize.

    The caller treats ``None`` as "fall through to NotImplementedError".

    ``forbidden_ids`` is the set of element ids already acted on by
    prior steps in the same scenario — the synthesizer refuses to
    emit an assertion against any of them so the Then step is forced
    to either match a distinct element or fall through to the LLM
    heal for a meaningful consequence assertion.
    """
    text = step.text or ""
    tokens = _normalize(text)
    is_negated = bool(_NEGATION_RE.search(text))
    forbidden = forbidden_ids or set()

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

    # 3. Assertion patterns. For Then steps, skip elements that a
    #    prior When step in the same scenario already acted on — a
    #    consequence assertion should target something NEW.
    element = _best_element_match(
        tokens,
        elements,
        forbidden_ids=forbidden if step.keyword == "Then" else None,
    )
    if element is not None:
        locator = f"{fixture_name}.locate({element.id!r})"
        for pattern, tpl in _ASSERTION_PATTERNS:
            if pattern.search(text):
                return tpl.format(locator=locator)
        # Fallback for pure `Then` steps that reference an element but
        # do not state a specific assertion — default to visibility.
        # Only for non-negated Then; "Then X is NOT <something>" must
        # not become "expect visible". We also require the matched
        # element to not be in `forbidden` (handled by the call above).
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


# ---------------------------------------------------------------------------
# Post-generation validator
#
# Applied to every final body (LLM or synthesized) before it's written
# to the step file. Catches three classes of bug the prompts forbid
# but models still occasionally ship:
#
#   1. absolute URL literals in to_have_url — environment-frozen and
#      will break against dev / staging / prod. REPAIR in place.
#   2. a Then step whose assertion targets the same element id a
#      prior When/And step already acted on — violates
#      CONSEQUENCE-NOT-TARGET. REJECT to NotImplementedError so heal
#      can rewrite it.
#   3. a pom_method whose name shares no meaningful token with the
#      step text (the "Filter button → search_assets" mis-bind).
#      REJECT to NotImplementedError.
# ---------------------------------------------------------------------------


_URL_LITERAL_RE = re.compile(
    r"""to_have_url\(\s*(['"])(https?://[^'"]+)\1\s*\)"""
)


_METHOD_CALL_RE = re.compile(
    r"^\s*[a-zA-Z_][a-zA-Z0-9_]*\.([a-zA-Z_][a-zA-Z0-9_]*)\s*\("
)


_LOCATE_ANY_ID_RE = re.compile(
    r"\.locate\(\s*(['\"])([^'\"]+)\1\s*\)"
)


# Visibility-shaped Then assertions. Used by rule 4
# (PREEXISTING-ELEMENT ASSERTION) to detect `expect(...).to_be_visible()`
# on an element that was already on the page at load.
_VISIBILITY_ASSERT_RE = re.compile(
    r"""expect\([^)]*\.locate\(\s*(['"])([^'"]+)\1\s*\)[^)]*\)"""
    r"\.to_be_visible\(\s*\)"
)


# Text-shaped Then assertions. Used by the weak-text-fallback rule
# to detect ``expect(page.get_by_text('Filter')).to_be_visible()`` and
# ``expect(page.get_by_role('heading', name='Filter')).to_be_visible()``
# patterns where the literal happens to match an element name that was
# already on the page at load (so the assertion passes trivially).
_GET_BY_TEXT_RE = re.compile(
    r"""get_by_text\(\s*(['"])([^'"]+)\1"""
)

_GET_BY_ROLE_NAME_RE = re.compile(
    r"""get_by_role\([^)]*\bname\s*=\s*(['"])([^'"]+)\1"""
)


# ``POMMethod.action`` values that count as state-changing. ``fill`` is
# deliberately NOT state-changing: filling a field proves nothing
# observable until a separate submit trigger fires the form.
_STATE_CHANGING_ACTIONS = frozenset({"click", "check", "select", "navigate"})


def nav_method_names(
    method_pairs: "Iterable[tuple[str, str | None]]",
    known_pages: list[dict] | None,
) -> set[str]:
    """POM method names that click/navigate toward a ``known_pages`` sibling.

    ``method_pairs`` is an iterable of ``(method_name, element_id)``.
    Using tuples instead of POMMethod/POMPlan lets ``prompts.py`` call
    this helper with the serialized dict form it already carries.

    A method counts as cross-page navigation when its name starts with
    ``click_`` or ``navigate`` AND either the method name or its
    element id contains one of the known_pages slugs as a substring
    (covers ``click_home`` → slug ``home`` and ``click_data_catalog``
    → slug ``catalog``).

    Two callers rely on this set:

    * :func:`_scenario_level_rejections` — decides whether a Then
      step is "preceded by a cross-page navigation", the only
      context in which asserting a preload-visible element still
      proves something.
    * :func:`autocoder.llm.prompts.build_steps_prompt` — surfaces
      the set to the step-binder LLM so it can self-avoid the
      PREEXISTING-ELEMENT ASSERTION ban at authoring time.
    """
    if not known_pages:
        return set()
    slugs = {(kp.get("slug") or "").lower() for kp in known_pages}
    slugs.discard("")
    if not slugs:
        return set()
    out: set[str] = set()
    for name, element_id in method_pairs:
        lname = (name or "").lower()
        if not lname.startswith("click_") and not lname.startswith("navigate"):
            continue
        eid = (element_id or "").lower()
        for slug in slugs:
            if slug in lname or slug in eid:
                out.add(name)
                break
    return out


def _nav_method_names(pom_plan: POMPlan, known_pages: list[dict] | None) -> set[str]:
    """POMPlan-shaped wrapper around :func:`nav_method_names`."""
    return nav_method_names(
        ((m.name, m.element_id) for m in pom_plan.methods),
        known_pages,
    )


def _scenario_level_rejections(
    feature_plan: FeaturePlan,
    method_action: dict[str, str],
    nav_method_names: set[str],
) -> tuple[set[str], set[str], set[str]]:
    """Per-step-text rejection sets for scenario-level validator rules.

    Returns ``(zero_action_then_texts, duplicate_step_texts,
    then_texts_without_nav_predecessor)``.

    A step text appears in the returned set only if **every** scenario
    in which it occurs triggers the rejection — this protects a step
    that is legitimate in one scenario but broken in another (step
    functions are shared across scenarios, so rejecting the body
    there would break the legitimate usage too).

    Duplicate-step rejection is the single exception: if a scenario
    contains two adjacent When/And steps with identical pom_method +
    args, the text of the SECOND occurrence is flagged regardless —
    the second call is guaranteed to be a no-op, so there is no
    "legitimate" usage to preserve.
    """
    per_step_zero_action: dict[str, list[bool]] = {}
    per_step_no_nav: dict[str, list[bool]] = {}
    duplicate_step_texts: set[str] = set()

    for scn in feature_plan.scenarios:
        resolved = _resolve_keywords(list(scn.steps))
        has_state_change = False
        saw_cross_page_nav = False
        prev_sig: tuple | None = None
        for s in resolved:
            is_state_change = False
            if s.pom_method:
                action = method_action.get(s.pom_method, "")
                if action in _STATE_CHANGING_ACTIONS:
                    is_state_change = True
                elif s.pom_method in nav_method_names:
                    is_state_change = True
            if is_state_change:
                has_state_change = True
            if s.pom_method and s.pom_method in nav_method_names:
                saw_cross_page_nav = True

            sig: tuple | None = None
            if s.keyword in ("When", "And") and s.pom_method is not None:
                sig = (s.pom_method, tuple(s.args or []))
                if prev_sig is not None and sig == prev_sig:
                    duplicate_step_texts.add(s.text or "")
            prev_sig = sig

            if s.keyword == "Then":
                per_step_zero_action.setdefault(s.text or "", []).append(
                    not has_state_change
                )
                per_step_no_nav.setdefault(s.text or "", []).append(
                    not saw_cross_page_nav
                )

    zero_action_then_texts = {
        t for t, flags in per_step_zero_action.items() if flags and all(flags)
    }
    then_texts_without_nav_predecessor = {
        t for t, flags in per_step_no_nav.items() if flags and all(flags)
    }
    return zero_action_then_texts, duplicate_step_texts, then_texts_without_nav_predecessor


# Methods whose names carry no content signal — exempt from the
# method-token overlap check because they apply to any step text.
_UNIVERSAL_POM_METHODS = {"navigate"}


# Generic verbs / nouns that appear in most method names and most
# step texts. Overlap via these alone isn't meaningful — if those
# are the ONLY tokens shared, the method is probably mis-bound.
_GENERIC_METHOD_TOKENS = {
    "open", "close", "show", "hide", "get", "do", "go", "handle",
    "click", "clicks", "fill", "fills", "check", "checks", "toggle",
    "select", "selects", "submit", "submits", "enter", "enters",
    "button", "buttons", "view", "manage",
}


def _repair_url_literal(body: str) -> str:
    """Rewrite ``to_have_url('https://host/path')`` to a path regex.

    ``expect(page).to_have_url(re.compile(r'/path(?:[/?#]|$)'))`` binds
    to the destination's path only, so the assertion stays correct
    across dev / staging / prod. The regex anchors on the path
    prefix; trailing query/fragment is tolerated.
    """
    def _sub(match: re.Match) -> str:
        url = match.group(2)
        parsed = urlparse(url)
        path = parsed.path or "/"
        path_esc = re.escape(path)
        return f"to_have_url(re.compile(r'{path_esc}(?:[/?#]|$)'))"
    return _URL_LITERAL_RE.sub(_sub, body)


def _binding_quality_reason(
    body: str,
    step: StepRef,
    forbidden_ids: set[str],
    pom_method_names: set[str],
    *,
    zero_action_then_texts: set[str] | None = None,
    duplicate_step_texts: set[str] | None = None,
    then_texts_without_nav_predecessor: set[str] | None = None,
    preload_visible_ids: set[str] | None = None,
    preload_visible_names: set[str] | None = None,
) -> str | None:
    """Return a one-word rejection reason, or ``None`` when the body is clean.

    Never called on bodies that already start with
    ``raise NotImplementedError`` — those are already queued for heal.

    Scenario-level rules (``zero_action_then_texts``,
    ``duplicate_step_texts``, ``then_texts_without_nav_predecessor``)
    are pre-computed once per feature by
    :func:`_scenario_level_rejections` and passed in — per-step
    inspection of the body then consults the sets.
    """
    # CONSEQUENCE-NOT-TARGET: a Then step may not bind to any id a
    # prior When/And step in the same scenario already acted on.
    if step.keyword == "Then" and forbidden_ids:
        for loc in _LOCATE_ANY_ID_RE.finditer(body):
            if loc.group(2) in forbidden_ids:
                return "consequence_not_target"

    # DUPLICATE-ACTION: a When/And step whose (pom_method, args) is
    # identical to the prior When/And step in its scenario. The
    # second call is always a no-op.
    if (
        duplicate_step_texts
        and step.keyword in ("When", "And")
        and (step.text or "") in duplicate_step_texts
    ):
        return "duplicate_action"

    # ZERO-ACTION SCENARIO: a Then step in a scenario whose Given→Then
    # path contains no state-changing action (click/check/select/
    # navigate). fill_* on its own is not state-changing — filling
    # a field proves nothing until something triggers the form.
    if (
        zero_action_then_texts
        and step.keyword == "Then"
        and (step.text or "") in zero_action_then_texts
    ):
        return "zero_action_scenario"

    # PREEXISTING-ELEMENT ASSERTION: a Then step whose body asserts
    # `expect(...).to_be_visible()` on an element id that was already
    # on the page at load (``preload_visible_ids``) AND whose scenario
    # was not preceded by a cross-page navigation. The assertion
    # would pass before any action ran, so it proves nothing.
    if (
        step.keyword == "Then"
        and preload_visible_ids
        and then_texts_without_nav_predecessor is not None
        and (step.text or "") in then_texts_without_nav_predecessor
    ):
        for m in _VISIBILITY_ASSERT_RE.finditer(body):
            if m.group(2) in preload_visible_ids:
                return "preexisting_element_assertion"

    # WEAK-TEXT-FALLBACK: a Then step whose body asserts visibility via
    # ``get_by_text('<literal>')`` or ``get_by_role(..., name='<literal>')``
    # where <literal> (case-insensitive) is a substring of a name that
    # was already on the page at load. Example: the Filter button has
    # name "Filter"; a Then body using ``get_by_text('Filter')`` would
    # match that button and pass trivially. Only fires on Then steps
    # that were NOT preceded by a cross-page navigation — on a fresh
    # destination page, a heading name that happens to share text with
    # the source page is still a real consequence of the nav.
    if (
        step.keyword == "Then"
        and preload_visible_names
        and then_texts_without_nav_predecessor is not None
        and (step.text or "") in then_texts_without_nav_predecessor
    ):
        preload_names_lower = {n.lower() for n in preload_visible_names if n}
        for pattern in (_GET_BY_TEXT_RE, _GET_BY_ROLE_NAME_RE):
            for m in pattern.finditer(body):
                literal = (m.group(2) or "").lower().strip()
                if not literal:
                    continue
                # Substring match in either direction — "Filter"
                # matches a name "Filter" AND "Filter Panel", and a
                # literal "Stewie assistant" matches a name "Open
                # Stewie assistant". Both directions are weak.
                for pname in preload_names_lower:
                    if literal in pname or pname in literal:
                        return "weak_text_fallback"

    # METHOD-STEP TOKEN MATCH: bound pom_method name must share >= 1
    # meaningful token with the step text. Generic verb tokens
    # (click/open/view/...) don't count — two step/method pairs that
    # share only 'click' are still mis-bound.
    m = _METHOD_CALL_RE.match(body)
    if m:
        method_name = m.group(1)
        if (
            method_name in pom_method_names
            and method_name not in _UNIVERSAL_POM_METHODS
        ):
            step_tokens = set(_normalize(step.text or ""))
            step_tokens -= _GENERIC_METHOD_TOKENS
            method_tokens = set(method_name.lower().split("_"))
            method_tokens -= _GENERIC_METHOD_TOKENS
            if step_tokens and method_tokens and not (step_tokens & method_tokens):
                return "method_token_mismatch"

    return None


def _body(
    step: StepRef,
    extracted_args: list[str],
    fixture_name: str,
    pom_args_by_method: dict[str, list[str]],
    elements: list[Element],
    *,
    forbidden_ids: set[str] | None = None,
    steps_plan: StepsPlan | None = None,
    pom_method_names: set[str] | None = None,
    element_ids: set[str] | None = None,
    zero_action_then_texts: set[str] | None = None,
    duplicate_step_texts: set[str] | None = None,
    then_texts_without_nav_predecessor: set[str] | None = None,
    preload_visible_ids: set[str] | None = None,
    preload_visible_names: set[str] | None = None,
) -> str:
    """Produce a step body, then run repair + rejection passes."""
    raw = _body_inner(
        step,
        extracted_args,
        fixture_name,
        pom_args_by_method,
        elements,
        forbidden_ids=forbidden_ids,
        steps_plan=steps_plan,
        pom_method_names=pom_method_names,
        element_ids=element_ids,
    )

    # Already a heal stub — don't second-guess it.
    if raw.lstrip().startswith("raise NotImplementedError"):
        return raw

    # REPAIR pass: rewrite absolute URL literals to path regexes.
    repaired = _repair_url_literal(raw)

    # REJECT pass: hard-rule violations downgrade to a NotImplementedError
    # stub so the heal stage gets a chance to rewrite the body instead
    # of shipping a silently-wrong assertion.
    reason = _binding_quality_reason(
        repaired,
        step,
        forbidden_ids or set(),
        pom_method_names or set(pom_args_by_method.keys()),
        zero_action_then_texts=zero_action_then_texts,
        duplicate_step_texts=duplicate_step_texts,
        then_texts_without_nav_predecessor=then_texts_without_nav_predecessor,
        preload_visible_ids=preload_visible_ids,
        preload_visible_names=preload_visible_names,
    )
    if reason:
        # The rejection reason is diagnostic metadata, not part of the
        # step's user-facing identity. Embedding it into the
        # NotImplementedError body string would leak internal validator
        # vocabulary into the heal prompt's context (heal reads the
        # body on the file-failure path), and in the worst case lets
        # the heal LLM pick up phrases like "zero_action_scenario" as
        # literal assertion text. Log the reason separately and keep
        # the body's string a clean ``Implement step: <verbatim>``.
        logger.info(
            "validator_rejected_binding",
            step_text=step.text,
            reason=reason,
            keyword=step.keyword,
        )
        return (
            f'raise NotImplementedError('
            f'"Implement step: {step.text}")'
        )
    return repaired


def _body_inner(
    step: StepRef,
    extracted_args: list[str],
    fixture_name: str,
    pom_args_by_method: dict[str, list[str]],
    elements: list[Element],
    *,
    forbidden_ids: set[str] | None = None,
    steps_plan: StepsPlan | None = None,
    pom_method_names: set[str] | None = None,
    element_ids: set[str] | None = None,
) -> str:
    # Prompt 3 (steps plan): if the LLM produced a body for this step
    # text, prefer it — but only after the same AST-level validator
    # that heal uses confirms the statement is safe. Invalid bodies
    # fall through to the deterministic synthesizer below, which in
    # turn falls through to `NotImplementedError` so the heal stage
    # can replace them on the first pytest failure.
    if steps_plan is not None:
        raw = steps_plan.body_for(step.text or "")
        if raw:
            # Local import avoids a circular import at module load.
            from autocoder.heal.validator import validate_body

            cleaned, errs = validate_body(
                raw,
                fixture_name=fixture_name,
                pom_method_names=pom_method_names or set(pom_args_by_method.keys()),
                element_ids=element_ids,
                max_statements=1,
            )
            # A bare `pass` on a Then step means the LLM couldn't find a
            # safe binding — but the heal scanner only picks up
            # `NotImplementedError`, so accepting `pass` would leave the
            # assertion silently green forever. Discard it and fall
            # through to the NotImplementedError emitter below.
            if (
                not errs
                and cleaned
                and not (step.keyword == "Then" and cleaned.strip() == "pass")
            ):
                return cleaned

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
        step,
        fixture_name,
        elements,
        list(pom_args_by_method.keys()),
        forbidden_ids=forbidden_ids,
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
    steps_plan: StepsPlan | None = None,
    known_pages: list[dict] | None = None,
) -> str:
    """Emit a step-definitions module covering every step in the plan.

    ``elements`` is the extraction element catalog. When provided, the
    renderer can synthesize executable step bodies for common
    assertion/navigation patterns instead of emitting
    ``NotImplementedError``.

    ``steps_plan`` is the optional prompt-3 output. When present, the
    renderer uses each binding's body (after AST validation) before
    falling back to heuristic synthesis — so every step has an
    LLM-written Playwright body unless it fails validation, in which
    case the deterministic path keeps the file runnable.

    ``known_pages`` is the sibling-page snapshot (same shape the
    feature-plan prompt receives). When provided, the scenario-level
    validator uses it to decide which pom_methods count as cross-page
    navigation — that matters for the PREEXISTING-ELEMENT ASSERTION
    rule, which only fires when a Then step lacks a cross-page
    predecessor.
    """
    fixture_name = pom_plan.fixture_name
    class_name = pom_plan.class_name
    pom_args_by_method = {m.name: list(m.args or []) for m in pom_plan.methods}
    method_to_element = {m.name: m.element_id for m in pom_plan.methods if m.element_id}
    method_action = {m.name: m.action for m in pom_plan.methods}
    el_list: list[Element] = list(elements or [])
    pom_method_names = set(pom_args_by_method.keys())
    element_ids = {e.id for e in el_list}
    preload_visible_ids = {e.id for e in el_list if e.visible}
    # Names of preload-visible elements, used by the weak-text-fallback
    # rule to reject ``get_by_text('<literal>')`` bindings whose literal
    # is a substring of a pre-existing UI element's name (e.g. the
    # Filter button's name is "Filter", so ``get_by_text('Filter')``
    # would match that button and pass trivially).
    #
    # Filter out names shorter than 3 characters: a 1-2 char name
    # (pagination button "2", icon "X") is too short to be a reliable
    # substring indicator. Without this filter, legit assertions like
    # ``get_by_text('Page 2')`` get over-rejected because "2" appears
    # as a substring of "page 2".
    preload_visible_names = {
        (e.name or "").strip()
        for e in el_list
        if e.visible and (e.name or "").strip() and len((e.name or "").strip()) >= 3
    }
    nav_method_names = _nav_method_names(pom_plan, known_pages)
    (
        zero_action_then_texts,
        duplicate_step_texts,
        then_texts_without_nav_predecessor,
    ) = _scenario_level_rejections(feature_plan, method_action, nav_method_names)

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

    # Per-step-text forbidden element ids — union of every element id
    # that was already acted on by a prior When/And step in any
    # scenario this text participates in. The Then-step synthesizer
    # uses this to avoid re-asserting the same element the scenario
    # just clicked / filled.
    forbidden_by_text: dict[str, set[str]] = {}
    for scn in feature_plan.scenarios:
        acted: set[str] = set()
        for step in _resolve_keywords(list(scn.steps)):
            if step.keyword == "Then":
                forbidden_by_text.setdefault(step.text, set()).update(acted)
                continue
            if step.pom_method and step.pom_method in method_to_element:
                acted.add(method_to_element[step.pom_method])
            else:
                match = _best_element_match(_normalize(step.text or ""), el_list)
                if match is not None:
                    acted.add(match.id)

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
        body = _body(
            step,
            extracted_args,
            fixture_name,
            pom_args_by_method,
            el_list,
            forbidden_ids=forbidden_by_text.get(text),
            steps_plan=steps_plan,
            pom_method_names=pom_method_names,
            element_ids=element_ids,
            zero_action_then_texts=zero_action_then_texts,
            duplicate_step_texts=duplicate_step_texts,
            then_texts_without_nav_predecessor=then_texts_without_nav_predecessor,
            preload_visible_ids=preload_visible_ids,
            preload_visible_names=preload_visible_names,
        )
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
