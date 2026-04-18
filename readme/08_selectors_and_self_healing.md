# 08 · Selectors and self-healing

A "stable selector" is the cheapest unambiguous locator we can find for
an element. Two halves of the system collaborate on this:

- **Generation time** — `autocoder/extract/selectors.py` picks one
  primary plus up to four fallbacks for every captured element.
- **Runtime** — `tests/support/locator_strategy.py` walks the fallback
  list when the primary fails.

Both halves use the same priority order. **Keep them in sync** when
you add or remove a strategy.

## Priority order

1. `test_id`     — `data-testid`, `data-test`, `data-cy`, `data-qa`,
                   `data-automation-id`. First non-empty wins.
2. `role_name`   — Playwright's `getByRole(role, name=...)`.
3. `label`       — `<label for>` text or `aria-label`.
4. `placeholder` — `placeholder` attribute.
5. `text`        — Anchor text for buttons/links/tabs/menuitems/headings.
6. `css`         — Stable `#id` or `[name=foo]` attribute selector.
7. `xpath`       — Generated positional xpath. Last resort.

Framework-generated ids (anything containing `:r`, `__react`, `mui-`,
`chakra-`) are skipped automatically — they shift between renders.

## Generation-time output

`build_selector(handle)` returns `(primary, fallbacks)` where:

```python
StableSelector(strategy="test_id", value="login-submit", role="button", name="Sign in")
```

Up to four other candidates from the priority list are kept as
fallbacks. The renderer puts both into the POM as a `SELECTORS` dict:

```python
class LoginPage(BasePage):
    SELECTORS = {
        "submit": [
            {"strategy": "test_id", "value": "login-submit"},
            {"strategy": "role_name", "value": "button", "name": "Sign in"},
            {"strategy": "css", "value": "form button[type=submit]"},
        ],
    }
```

## Runtime resolver

When a generated POM method calls `self.locate("submit")`, it routes to
the resolver:

```python
def resolve(page, specs, *, timeout_ms=4000) -> Locator:
    for spec in specs:
        locator = _build_locator(page, spec)
        try:
            locator.first.wait_for(state="attached", timeout=timeout_ms)
            return locator.first
        except Exception:
            continue
    raise LocatorNotFound(...)
```

If the primary fails, the resolver moves to the next fallback and
records the failed attempt. When every selector misses, it raises
`LocatorNotFound` with the full diagnostic chain so you can see which
strategies were tried and why each failed.

## Why a chain is not magic

The chain only protects against changes that any *other* strategy in
the list still resolves. Concrete examples:

- A test_id rename → still found via role+name.
- A label edit → still found via test_id (if present).
- An entire element removal → no chain saves you. The test will fail
  loudly with `LocatorNotFound` and the failing element id.

That is the right trade-off: the system absorbs cosmetic drift but
surfaces structural change as a real test failure.

## Patching a flaky page by hand

The `SELECTORS` dictionary at the top of every generated POM is the
explicit hand-edit point. Replacing or reordering selectors there is
safe — the orchestrator preserves it on re-render unless the
extraction fingerprint actually changes the catalog. If you need a
permanent override, lift the change into `autocoder/extract/selectors.py`
so the next extraction emits it directly.
