# 14 · Extending the system

The system is built so that the common changes are local to one or two
files. This doc lists the most common ones and where to make them.

## Add a new selector strategy

Two files must change in lock-step:

1. `autocoder/extract/selectors.py` — add a new `SelectorStrategy`
   value, extend `build_selector(...)` to detect and rank it, and
   update `to_playwright_call(...)` so the generation-time renderer
   can serialise it.
2. `tests/support/locator_strategy.py` — extend `_build_locator(...)`
   so the runtime resolver can build the same locator from a `dict`
   spec.

Keep the priority order in `selectors.py` matched by the order the
runtime resolver receives specs. Resolver iteration order is the sole
source of priority at runtime.

## Add a new scenario tier

1. `autocoder/generate/feature.py` — add the tier to `_TIER_TAG` so
   the renderer knows which Gherkin tag(s) to emit.
2. `autocoder/cli.py` — add the tier to the `--tier` choice list on
   `generate` and `extend`.
3. `pytest.ini` — register the matching marker.
4. `autocoder/llm/prompts.py` — append the tier to the
   `FEATURE_SYSTEM` schema enum if you want the LLM to use it
   directly (not strictly required; the validator only enforces tier
   names that the renderer supports).

## Add a new POM action

1. `autocoder/llm/validator.py` — add the verb to `_VALID_ACTIONS`
   (and to `_FILL_LIKE` if it takes a value argument).
2. `autocoder/generate/pom.py` — add a branch in `_build_method_body`
   and (if it needs a parameter) in `_build_method_params`.
3. `autocoder/llm/prompts.py` — add the verb to the action enum in
   `POM_SYSTEM`.

## Swap the LLM model

Change `OLLAMA_MODEL` in `.env`. Nothing else is required as long as
the model can return clean JSON in `format=json` mode. For non-Ollama
backends, write a sibling of `autocoder/llm/ollama_client.py` that
exposes the same `chat_json(system, user, purpose=...)` signature
and select it in `autocoder/llm/plans.py` and
`autocoder/heal/runner.py`.

## Allow a new heal pattern

`autocoder/heal/validator.py` controls what the LLM is allowed to
emit as a step body. To accept a new pattern (e.g. a Playwright
primitive that's not yet on the allow-list):

1. Update `_BUILTIN_FIXTURE_ATTRS` if the new pattern adds a
   first-party method on the POM fixture surface.
2. Add the example to `HEAL_SYSTEM` / `FAILURE_HEAL_SYSTEM` so the
   model knows it's available.
3. Add a unit test in `tests/unit/test_heal.py` (stub heal) or
   `tests/unit/test_heal_failures.py` (failure heal).

To recognise a new pytest failure shape, add a row to
`_FAILURE_PATTERNS` in `autocoder/heal/pytest_failures.py` and a
matching hint in `FAILURE_HEAL_SYSTEM`.

## Add a new orchestrator stage

The orchestrator is intentionally linear — each stage reads typed
inputs and writes typed outputs. To insert a stage:

1. Define its inputs/outputs as Pydantic models in
   `autocoder/models.py`.
2. Implement the stage as a function in a new module under the
   appropriate package (`extract/`, `llm/`, `generate/`,
   `manifest/`).
3. Wire it into `autocoder/orchestrator.py` between the existing
   stages.
4. Add a status value to `models.Status` if the stage represents a
   distinct lifecycle step.

## Capture additional element metadata

`autocoder/extract/inspector.py:_INTERACTIVE_SELECTOR` is the
element-class allowlist. Widen it to capture more roles. Update
`_kind_for(...)` if the new kinds need their own action mapping. Bump
`MAX_ELEMENTS_PER_PAGE` in `.env` for dense pages — but consider
filtering instead, since extraction size is the largest token-cost
lever downstream.

## Add a multi-step "flow" abstraction

The current renderer emits one step → one POM method. A higher-level
"flow" (e.g. `complete_login_flow`) is straightforward: write a method
on the relevant POM and reference it from a step in
`tests/features/<slug>.feature`. The step generator will see the
method via the validator's POM method list and wire it up.

For a fully reusable flow catalog (login as a flow that any feature
can compose), add a new model in `autocoder/models.py` (e.g.
`FlowSpec`), persist it in the registry, and inject the available flow
names into the feature-plan prompt. The legacy notes
(`info/06_optimized_architecture.md`) sketch the design.

## Replace the runtime self-heal

If you want a third-party self-healing library (e.g. a vision-based
healer), implement it in `tests/support/locator_strategy.py:resolve`
and keep the same function signature. Generated POMs only depend on
that function, so the rest of the system is unchanged.

## Where it would not be easy to extend

- **Multi-tab / multi-window flows.** The orchestrator opens one
  page per session. Multi-page flows require a new fixture
  abstraction in `tests/support/`.
- **Mobile viewports.** Add a Playwright `device` argument to
  `extract/browser.py` and to `tests/conftest.py:browser_context_args`.
- **Network mocking.** Out of scope today; would need a recording
  layer between the inspector and the LLM call.
