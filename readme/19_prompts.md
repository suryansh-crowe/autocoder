# 19 · The JSON-backed prompt library

Every system prompt the agent sends to the LLM lives as a standalone
JSON file under `src/autocoder/prompts/`, not as a triple-quoted
Python string. The Python modules that used to hold those strings
are now thin loaders — they read the JSON at import and re-publish
the `system` field under the same constant names every call site
already expects.

Why: prompts are the highest-iteration surface of the whole tool.
Keeping them as data means a product manager, QA engineer, or prompt
engineer can tweak them without touching Python, rerun, and see the
effect in minutes. Git diffs on a prompt edit stay readable — one
file, one change, no code review surface.

## Layout

```
src/autocoder/prompts/
├── __init__.py          # loader — load_system(name), available_names()
├── index.json           # human-readable catalogue of every prompt
├── pom_plan.json        # prompt 1 — controls → POM method plan
├── feature_plan.json    # prompt 2 — controls + POM → Gherkin scenarios
├── steps_plan.json      # prompt 3 — feature → Playwright body per step
├── heal_stub.json       # fill NotImplementedError stubs
└── heal_failure.json    # rewrite failing test bodies (runtime heal)
```

## File shape

Each prompt JSON has the same keys:

```json
{
  "name": "feature_plan",
  "version": 1,
  "description": "Prompt 2 — turn the control JSON + POM method list into Gherkin scenarios …",
  "source_module": "src/autocoder/llm/prompts.py",
  "source_constant": "FEATURE_SYSTEM",
  "system": "You are a planner for a Playwright BDD generator.\n…"
}
```

Only `system` is load-bearing at runtime. `name`, `version`,
`description`, `source_module`, `source_constant` are metadata for
humans and tooling.

## How the code reads them

`src/autocoder/prompts/__init__.py` exposes two functions:

```python
from autocoder.prompts import load_system, available_names

POM_SYSTEM   = load_system("pom_plan")        # same string you'd get from the old constant
every_prompt = available_names()              # ["feature_plan", "heal_failure", …]
```

The constants published by `autocoder/llm/prompts.py` and
`autocoder/heal/prompts.py` are now just:

```python
# src/autocoder/llm/prompts.py
POM_SYSTEM     = load_system("pom_plan")
FEATURE_SYSTEM = load_system("feature_plan")
STEPS_SYSTEM   = load_system("steps_plan")

# src/autocoder/heal/prompts.py
HEAL_SYSTEM         = load_system("heal_stub")
FAILURE_HEAL_SYSTEM = load_system("heal_failure")
```

Every downstream import (`generate_pom_plan`, `heal_steps`, etc.)
is unchanged.

## Editing a prompt

1. Open the JSON file in any editor — no Python runtime needed.
2. Change the `system` field. Newlines inside a JSON string are
   written as `\n`; most editors unfold them visually.
3. Save.
4. Next `autocoder run` picks it up automatically. The loader has
   `lru_cache`, but the cache is per-process, so every fresh
   invocation re-reads.

### Validate after an edit

```bash
python -c "import sys; sys.path.insert(0, 'src'); \
  from autocoder.prompts import load_system; \
  print(load_system('feature_plan')[:400])"
```

Any JSON syntax error surfaces immediately as a `PromptNotFound`
exception with the path of the broken file.

### Bust the plan cache

The agent caches the LLM's *output* under
`manifest/plans/<fixture>.<kind>.<fingerprint>.json`. An edit to a
prompt doesn't change the fingerprint — so a rerun will silently
use the old cached output.

Force a fresh LLM call:

```bash
autocoder run --urls-file urls.txt --force
```

Or wipe the plan cache directly: `rm -rf manifest/plans/`.

## A/B testing a variant

Make a sibling file, wire the constant to it temporarily:

```bash
cp src/autocoder/prompts/feature_plan.json \
   src/autocoder/prompts/feature_plan_v2.json
# edit feature_plan_v2.json, then in src/autocoder/llm/prompts.py:
#   FEATURE_SYSTEM = load_system("feature_plan_v2")
autocoder run --urls-file urls.txt --force
diff tests/features/login.feature <(git show HEAD:tests/features/login.feature)
```

Restore the old constant when done.

## Safety

If a prompt file is missing, malformed, or has no non-empty `system`
field, `load_system` raises `PromptNotFound` at *import* time — so
you see the error the moment the Python module tries to load, with
the list of available prompt names in the exception message. There
is no silent fallback to a blank or default prompt.

## Editing guidance per prompt

Brief, per-file cheatsheet. See each file's `system` field for the
full current contract.

### `pom_plan.json`

Shapes the Page Object method list. Best place to change method
naming conventions, action verb mapping (click/fill/check/select),
method count caps. Changes here cascade into every downstream
prompt — the feature and steps prompts reference the method names
this prompt picks.

### `feature_plan.json`

Shapes the scenarios. By far the biggest prompt — this is where
the per-control-type test catalogue, scenario title quality rules,
data specificity bans, and rich-assertion preferences live. If your
generated `.feature` files look generic, this is the file to edit.

### `steps_plan.json`

Shapes the per-step Playwright body. Mostly a safety net — it
re-enforces the data-specificity and rich-assertion rules from
`feature_plan.json` at the implementation layer, and adds the
placeholder-string detection that will refuse to pass `'valid value'`
or `'test'` through to a generated test. Edit here when you want to
extend the allowed statement grammar (new Playwright primitives) or
add more text-pattern → `expect()` mappings.

### `heal_stub.json`

Runs when a generated test body ended up as `NotImplementedError`.
Single-statement output, same grammar as `steps_plan.json`. Conservative
by design — the envelope includes `forbidden_element_ids` so a heal
never re-asserts the same element a When-step just clicked.

### `heal_failure.json`

Runs when a generated test actually threw at runtime. Multi-statement
(up to 5) so a fix like "check the consent checkbox *then* retry the
click" is expressible. Carries `failure_class` (timeout / disabled /
intercepted / wrong_kind / locator_not_found / …) so the model can
reason about prerequisites.

## Versioning

Each JSON file has a `version` integer. It's not used by the loader
today — it's there so future tooling can route prompt changes
through A/B or canary. Bump it when you make a prompt change you
consider breaking, so experiments can pin a specific version.

## Relationship to the plan cache

The plan cache key is
`(fixture, extraction_fingerprint [, tiers, feature_fingerprint])`.
Prompt contents do NOT go into the key. That is deliberate:
- The plan cache answers "given this page's controls, what did the
  LLM say?" — not "given these controls AND this prompt, what did
  it say?".
- A prompt edit invalidates *intent*, not *structure*, so the
  correct response is `--force`.

If you prefer prompt-edit-triggered invalidation, hash the
prompt content and include it in the key — ~10 lines in
`autocoder/llm/plans.py`. Not done by default because prompt
iteration is an author-time action, not a per-run one.

## Tips

* Keep each prompt under ~8 KB (~2000 tokens in). Bigger prompts
  mean more input tokens per call × 8 URLs × every rerun.
* Don't embed few-shot examples here unless strictly necessary —
  they compound the per-call token cost fast.
* When you add a new behaviour rule, add a matching anti-pattern
  example. Models respond to contrast better than to prescription
  alone.
* For any rule you add, think whether it's enforced by the
  validator downstream (`autocoder/llm/validator.py`,
  `autocoder/heal/validator.py`). Prompts alone are advisory;
  validators are absolute.
