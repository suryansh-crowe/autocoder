# 01 · Overview

## What the system is

A Python orchestrator that takes a list of URLs and produces a complete
Playwright pytest-bdd test suite for them. It runs end-to-end with no
hand authoring of POMs, features, or step definitions for the URLs you
hand it.

```
URL list  ──▶  classify  ──▶  auth-first  ──▶  extract  ──▶
              POM plan  ──▶  POM render   ──▶  feature plan  ──▶
              feature render  ──▶  steps render  ──▶  registry
```

A single LLM model — **Phi-4 14B via Ollama**, CPU-only — drives the
two planning calls. Everything else is deterministic Python.

## What it is not

- It is **not** a one-shot script that emits tests for one URL and
  exits. The system is reusable: invoke it on the same URLs again,
  on new URLs, or to add tiers to an existing URL, and it picks up
  exactly where it left off.
- It is **not** a code generator that asks the LLM to write Python.
  The LLM only emits short JSON action plans. Templates render the
  Python deterministically, so syntax errors are structurally
  impossible and hallucinated method names are caught by grammar
  validation before any file is written.
- It is **not** a black-box. Every artifact lives on disk in a
  human-readable form: `manifest/registry.yaml`, per-URL extraction
  JSON, plan JSON, generated `.py` and `.feature` files.

## Design principles

1. **Browser observes truth.** Anything that can be derived from
   loading a real URL in Playwright is derived that way — never asked
   from the LLM.
2. **Deterministic by default, LLM by exception.** The LLM is the
   only place we accept variability, and it sees the smallest
   possible context that still lets it choose well.
3. **Resumable from any failure.** Each URL has a status in the
   registry. If a run dies, the next run continues at the first
   incomplete stage for each URL.
4. **No secrets near the LLM.** Credentials only live in the local
   `.env`. Generated code reads them via `os.environ.get(...)`. The
   LLM never sees them, and they are never written to logs, manifest,
   or generated artifacts.
5. **Self-healing locators.** Every element is captured with a
   primary selector plus up to four fallbacks. The runtime resolver
   walks the chain, so a single brittle attribute does not break the
   suite.

## Capabilities the system supports

- URL intake from four sources (CLI args > `--urls-file` >
  `AUTOCODER_URLS` env > `[LOGIN_URL, BASE_URL]` from `.env`),
  with structure-aware splitting that preserves URLs whose query
  strings contain commas.
- URL classification (public / authenticated / redirect-to-login /
  login) and dependency mapping (login first, redirect targets
  before sources).
- Auth-first generation: an `auth_setup` test is rendered before
  protected pages are explored.
- DOM/UI extraction limited to interactive elements; element kind
  honours `<input type=...>` so checkboxes/radios/submits get the
  right Playwright primitive.
- Stable selector discovery with up to four fallbacks per element.
- POM creation/update with selector dictionaries kept in one place.
- BDD feature generation by tier (smoke / sanity / regression / etc).
- Playwright step generation that calls POM methods, with explicit
  `NotImplementedError` stubs for steps that cannot be safely bound.
- **Heal stage** (`autocoder heal`) — fills the stubs via the LLM
  with AST-validated single-statement bodies.
- **Runtime-failure heal** (`autocoder heal --from-pytest`) — runs
  pytest, captures Playwright errors, asks the LLM for revised
  bodies (up to 5 statements so prerequisites are expressible).
- Tracking and resume via `manifest/registry.yaml`. Per-URL
  failures don't abort the whole run — failed URLs are marked and
  the loop continues.
- Rerun awareness: extractions and plans are fingerprinted;
  unchanged pages skip the LLM entirely.
- Coverage extension on existing URLs without duplicating scenarios.
- Local-only verification: `python scripts/verify_local_llm.py`
  records every outbound TCP destination during a real `/api/chat`
  and asserts loopback / private-network only.
