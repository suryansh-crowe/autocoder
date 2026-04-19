# 07 · Extraction

The extraction stage opens each URL in a real browser and produces a
compact `PageExtraction`. The catalog is deliberately small — we never
dump the full DOM or the full accessibility tree. That choice is the
single biggest lever on token cost downstream.

## Browser session

`autocoder/extract/browser.py` is a context manager around the
Playwright sync API, plus a resilient-navigation helper:

```python
with open_session(settings, use_storage_state=True) as sess:
    diag = goto_resilient(sess.page, url,
                          nav_timeout_ms=settings.browser.extraction_nav_timeout_ms,
                          diagnostics_dir=settings.paths.logs_dir)
    extraction = extract_page(sess.page, url=url, settings=settings, ...)
```

`goto_resilient` replaces a raw `page.goto(..., wait_until="domcontentloaded")`
with a tiered wait strategy:

1. `commit` — fires on first response byte. Robust against SPAs that
   redirect to a different origin before the original DOM parses.
2. best-effort `domcontentloaded` (bounded to half the nav timeout).
3. best-effort `networkidle` (bounded to 5 s).

If the `commit` tier itself times out, the helper dumps a screenshot
and the raw HTML to `manifest/logs/nav_timeout_<ts>_<host>.png|.html`
and raises `AuthUnreachable`, which the orchestrator reads to decide
whether to escalate into the auth-first flow.

`use_storage_state=True` injects `.auth/user.json` if it exists, so
protected pages can be explored. When auth has been captured during
the current run (`registry.auth.status == STEPS_READY`), the
orchestrator sets `use_storage_state=True` for **every non-LOGIN
node**, including those the classifier labelled `PUBLIC` — the
anonymous verdict does not prove the authenticated view is
identical. When the file is missing, the session falls back to
anonymous and the orchestrator logs a warning rather than crashing.

## What the inspector captures

`autocoder/extract/inspector.py` walks the page once and emits a
`PageExtraction` Pydantic model with:

| Field            | What it is                                                    |
|------------------|---------------------------------------------------------------|
| `url`            | The URL the orchestrator asked for.                           |
| `final_url`      | Where the browser actually ended up (post redirects).         |
| `title`          | `<title>` after load.                                         |
| `kind`           | The classified `URLKind`.                                     |
| `requires_auth`  | True when the orchestrator entered with `storage_state`.      |
| `redirected_to`  | Set when `final_url != url`.                                  |
| `elements`       | The interactive-element catalog (capped, see below).          |
| `forms`          | One `FormSpec` per `<form>` with field IDs + submit ID.       |
| `headings`       | Up to 8 visible headings (h1/h2/h3/role=heading).             |
| `fingerprint`    | SHA-256 over `elements + headings + forms + title`.           |

## The element catalog

We enumerate elements matched by an interactive selector union:

```text
button, a[href], input:not([type=hidden]), textarea, select,
[role=button|link|tab|menuitem|checkbox|radio|switch|combobox|textbox],
[contenteditable=true]
```

For each visible match (cap: `MAX_ELEMENTS_PER_PAGE`, default 60):

1. Build a primary selector and up to four fallbacks (see
   `08_selectors_and_self_healing.md`).
2. Compute a kind (`button` / `link` / `input` / `select` / …).
3. Build a unique element id seeded from the accessible name (or tag +
   index). Names collide deterministically with a numeric suffix.
4. Capture `enabled` and `visible` flags.

The resulting `Element` rows are what the LLM sees in stage 4 (POM
plan) and stage 6 (feature plan).

## Forms

Each `<form>` becomes a `FormSpec`. Fields and the submit affordance
reference element ids that already exist in `elements`. This lets the
LLM cluster related methods (e.g. fill a whole form) without inventing
extra references.

## The fingerprint

`autocoder/registry/store.py` computes a stable SHA-256 over the
JSON-serialised payload of `(elements, headings, forms, title)`. The
fingerprint is the cache key for both the on-disk plan files and the
"skip if unchanged" rerun decision.

A small change in selector value changes the fingerprint. A pure
visual change (CSS, copy of a heading) does too. A change that does
*not* affect captured fields (a tweak to a footer image) does not.
That heuristic is intentionally on the eager side — false invalidation
only costs two LLM calls; false reuse risks shipping a stale POM.

## Extraction output

A persisted extraction looks like:

```json
{
  "url": "https://app.example.com/dashboard",
  "final_url": "https://app.example.com/dashboard",
  "title": "Dashboard",
  "kind": "authenticated",
  "requires_auth": true,
  "elements": [
    {
      "id": "search",
      "role": "textbox",
      "name": "Search assets...",
      "kind": "input",
      "selector": {"strategy": "test_id", "value": "search-input"},
      "fallbacks": [
        {"strategy": "role_name", "value": "textbox", "name": "Search assets..."},
        {"strategy": "placeholder", "value": "Search assets..."}
      ],
      "visible": true,
      "enabled": true
    }
  ],
  "forms": [],
  "headings": ["Dashboard", "My assets"],
  "fingerprint": "9b2f3c4d5e6f7081"
}
```

These files live at `manifest/extractions/<slug>.json` and are the
canonical input to the LLM stage.
