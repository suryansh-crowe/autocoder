# 20 · Application defects vs. script failures

Every pytest failure this system sees is one of three things. The
classifier at `src/autocoder/heal/pytest_failures.py:classify_origin`
separates them so the **heal engine only rewrites tests that are
actually buggy** — real application defects bubble through to the
report instead of being silently masked.

| origin | Meaning | Heal action |
|--------|---------|-------------|
| `script` | Bug in the generated test code — the LLM hallucinated an element id, emitted a chain like `.to_be_visible().click()`, mixed up Playwright primitives, guessed the wrong URL, etc. | Heal (rewrite the body) |
| `frontend` | Real application defect — the element the test targets was captured at extraction time but the running app no longer exposes it, an HTTP 5xx appears in the traceback, a navigation fails because of a server error. | **Skip heal.** Log, write to `manifest/runs/defects.json`, surface in the report's "Application defects" section. |
| `ambiguous` | Signal is unclear (e.g. `disabled` without enough traceback context, a generic `timeout`). | Heal conservatively — the LLM can still try. |

## Why this matters

Without the split, the autoheal plugin tries to rewrite EVERY failing
step body through the LLM. If the real failure is "the app's chat
button is broken", the LLM typically reacts by emitting a `pass`
fallback or a different wrong assertion — and the app bug disappears
from the report. The test goes green and nobody notices the
production regression.

With the split:

- The chat button appearing broken at runtime (`LocatorNotFound` on
  an id that IS in `manifest/extractions/catalog.json`) gets classified
  as `frontend`, logged to `manifest/runs/defects.json`, and rendered
  in the HTML report's "Application defects" section.
- The LLM guessing `expect(…).locate('validation_message')` on a
  catalog that has no such id gets classified as `script`, healed,
  rejected by the validator's hallucinated-id guard, and replaced
  with `pass  # no safe binding — …`.

## Decision tree

`classify_origin` in `pytest_failures.py` applies these in order,
first match wins:

1. **Python exception types** in the `type` attribute or traceback
   (`AttributeError`, `NameError`, `SyntaxError`, `KeyError`,
   `ImportError`, `ModuleNotFoundError`, `IndentationError`,
   `TabError`) → **script**. The running app cannot produce these;
   they originate in the generated Python code.
2. **Frontend signals** — regex hits against `HTTP 5\d{2}`,
   `status 5\d{2}`, `net::ERR_(CONNECTION|NAME|INTERNET|TUNNEL)`,
   `Cannot (GET|POST)`, `Application Error` — → **frontend**.
3. **`locator_not_found` / `not_attached` / `not_visible`**: the
   decision hinges on whether the referenced element id is in the
   current extraction catalog (`manifest/extractions/<slug>.json`):
   - id **in catalog** → **frontend** (extractor saw it, runtime
     doesn't → UI changed).
   - id **not in catalog** → **script** (LLM invented or mis-picked).
   - no id extractable from the traceback → **ambiguous**.
4. **`wrong_kind`** (`.fill()` on a button, `.check()` on a textbox,
   etc.) → **script** — the generated code chose the wrong
   Playwright primitive.
5. **`disabled`** / **`intercepted`** / generic **`timeout`** →
   **ambiguous**. These could be a missing prerequisite click
   (script) or a genuine app state bug (frontend). Default to
   ambiguous so the heal LLM can still take a shot.

The referenced element id is extracted via
`_extract_referenced_element_id` — it parses the error message +
traceback for `locate('<id>')` and `element_id '<id>'` patterns.

## Report surface

`autocoder report` now renders a dedicated **Application defects**
table below per-scenario results:

```
slug   | step / test                          | element             | class             | error
-------+--------------------------------------+---------------------+-------------------+----------------
catalog| _the_stewie_chat_panel_is_displayed  | open_stewie_assistant | locator_not_found | no selector resolved …
```

The terminal view (via `_print_report`) includes the same table.
`autocoder report --json` has a top-level `defects: [...]` array
with the same fields for CI dashboards.

`manifest/runs/defects.json` is the source of truth — overwritten
every time `_heal_failures` runs. Shape:

```json
{
  "catalog": [
    {
      "test_id": "tests.steps.test_catalog::test_click_ask_stewie_and_verify_chat_panel",
      "step_function": "_the_stewie_chat_panel_is_displayed",
      "error_type": "LocatorNotFound",
      "error_message": "no selector resolved ...",
      "failure_class": "locator_not_found",
      "element_id": "open_stewie_assistant"
    }
  ]
}
```

## Events to grep for

```
frontend_failure_detected    # per-failure, logged once with hint
frontend_defects_logged      # per-run summary; path + slug count
```

Both fire inside `_heal_failures`, so they only appear during
failure-heal (not stub heal). `heal_from_pytest_start` now also
logs `frontend_skipped=<N>` alongside `failures=<N>` so the total
failure count tracks both buckets.

## Tuning the classifier

When you want the classifier to treat a new error pattern as frontend,
add a regex to `_FRONTEND_ERROR_PATTERNS` in `pytest_failures.py`.
When you want a new Python exception class to count as script, add it
to `_SCRIPT_ERROR_TYPES`.

Don't remove the **ambiguous** bucket. For `disabled` / `intercepted`
/ generic `timeout`, the decision legitimately could go either way,
and defaulting to ambiguous preserves heal coverage for real script
bugs while surfacing enough context that a human can tell which it
was.
