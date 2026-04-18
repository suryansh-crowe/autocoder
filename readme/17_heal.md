# 17 · Heal — auto-fill step stubs via the local LLM

The renderer never silently passes a step it could not bind to a POM
method — it leaves an explicit
`raise NotImplementedError("Implement step: …")` stub. After you run
the suite once and see what's missing, run **`autocoder heal`** to
fill those stubs in via Phi-4.

```bash
autocoder heal             # heal every stub in tests/steps/
autocoder heal --slug login         # restrict to test_login.py
autocoder heal --dry-run            # preview suggestions; do not write
autocoder heal --force              # bypass cache; re-call the LLM
```

## What it does

1. **Scans** `tests/steps/test_*.py` for the renderer's exact stub
   shape (1-statement body whose only statement is
   `raise NotImplementedError("Implement step: …")`). Hand-edited
   bodies and multi-statement bodies are **left alone**.
2. **Loads context** for each stub: the cached POM plan
   (`manifest/plans/<slug>_page.pom.<fp>.json`) and the extraction
   snapshot (`manifest/extractions/<slug>.json`). The LLM sees the
   real method list and the real element catalog — never a guess.
3. **One LLM call per stub** asking for a single Python statement.
   Prompt + response are JSON; ~250 in / ~50 out per stub.
4. **Validates** the suggestion via AST. Rejected if it is not
   exactly one statement, references a non-existent POM method, or
   contains any of: `import`, `def`, `class`, `with`, `for`,
   `while`, `try`, `lambda`, comprehensions, exec/eval. Rejected
   suggestions are logged as `heal_invalid_body` and the stub is
   left in place.
5. **Applies** the validated body by line-replacement, then
   re-parses the whole file as a sanity check. If the rewritten
   file fails to parse, the change is aborted and the original is
   kept untouched.
6. **Caches** by `(slug, step_text, page_fingerprint, pom_method_set)`
   under `manifest/heals/`. Reruns of unchanged stubs spend zero
   tokens.

## What the LLM is allowed to emit

The validator's accept set (anything else is rejected and logged):

| Pattern | Use |
|---------|-----|
| `<fixture>.<method>(...)` where method is in pom_methods | bind to a generated POM method |
| `<fixture>.navigate()` | go to the page's canonical URL |
| `<fixture>.locate('<id>')` | resolve via the self-healing locator |
| `<fixture>.page.<playwright_method>(...)` | drop down to raw Playwright |
| `expect(<fixture>.locate(...)).to_be_visible()` etc. | Playwright assertion |
| `expect(<fixture>.page).to_have_url(...)` | URL assertion |
| `pass` | model has nothing useful to say |

`pass` is allowed but discouraged — the system prompt instructs the
model to use it only when nothing fits.

## Typical workflow

```bash
# generate the suite
autocoder generate

# run the suite once — see which stubs failed
pytest tests/steps/test_login.py

# heal the stubs (one LLM call per stub, ~200s each on Phi-4 CPU)
autocoder heal --slug login

# rerun
pytest tests/steps/test_login.py
```

If the model's heal still doesn't match what your app expects, edit
the body by hand. The next `autocoder heal --slug login` will see
your edit (it's no longer the renderer-shape stub) and skip it. Your
edit is preserved across reruns.

## Logs and tokens

Every heal call lands in `manifest/runs.log` with the same
`llm_call` schema as POM/feature plans, plus a `purpose` field
shaped `heal:<slug>:<function_name>` so you can filter:

```bash
jq -r 'select(.event=="llm_call" and (.purpose|startswith("heal:"))) |
       "\(.purpose)  in=\(.in_tokens) out=\(.out_tokens) cached=\(.cached)"' \
   manifest/runs.log
```

Stage-level events: `heal_start`, `heal_context_loaded` per slug,
`heal_applied` / `heal_dry_run` / `heal_invalid_body` per stub,
`heal_done` at the end.

## Safety guarantees

- **Hand edits survive.** Anything that isn't the renderer's exact
  stub shape is skipped.
- **No unsafe code is ever written.** The validator rejects
  imports, function/class definitions, lambdas, comprehensions,
  multi-statement bodies, syntax errors, and any reference to a
  POM method that doesn't actually exist.
- **No partial writes.** If the rewritten file does not re-parse,
  the original file is left untouched (the applier raises before
  any disk write).
- **No secrets in suggestions.** The heal prompt only ships the
  step text + POM method names + element catalog — the same
  redaction rules as `readme/15_logging.md` apply.

## CLI options

| Flag | Meaning |
|------|---------|
| `--slug <name>` | Restrict to `tests/steps/test_<slug>.py` only. |
| `--dry-run` | Show suggestions in the result table; do not write any file. |
| `--force` | Bypass the on-disk cache; re-call the LLM for every stub. |
