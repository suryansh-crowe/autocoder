# 01 · Overview

## What the system is

A Python orchestrator that takes a list of URLs and produces a complete
Playwright pytest-bdd test suite for them. It runs end-to-end with no
hand authoring of POMs, features, or step definitions for the URLs you
hand it.

```
URL list  ──▶  classify  ──▶  auth-first  ──▶  extract  ──▶
              prompt-1 (POM plan)   ──▶  POM render         ──▶
              prompt-2 (feature plan) ──▶  feature render   ──▶
              prompt-3 (steps plan)  ──▶  steps render      ──▶
              (heal stubs)          ──▶  registry
```

Three sequential LLM calls per URL, with the output of each one
consumed by the next. The system prompts themselves live as JSON
files under `src/autocoder/prompts/` so non-engineers can edit them.
Two backends are supported:

* **Phi-4 14B via Ollama**, CPU-only, local and free.
* **Azure OpenAI** (default `gpt-4.1`), hosted, fast.

Everything else in the pipeline is deterministic Python.

## What it is not

- It is **not** an exhaustive test generator. The feature-plan
  prompt caps scenarios at 3–8 per URL, 6 steps each, and up to 20
  POM methods. Default tiers are `smoke,happy,validation`. Add
  more via `--tier`, but the LLM still picks what fits those caps.
  See `10_generation.md` for the full coverage contract.
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
   `.env`. Every test-side module reads them via a single loader at
   `tests/settings.py` — no file under `tests/` touches `os.environ`
   directly. The LLM never sees them, and they are never written to
   logs, manifest, or generated artifacts.
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
- Resilient navigation: `goto_resilient` commits on first byte, then
  best-effort escalates to `domcontentloaded` and `networkidle`.
  Raises `AuthUnreachable` with a diagnostics dump (redirect chain,
  popups, console errors, screenshot + HTML) when even `commit`
  times out.
- Auth-first generation that automatically detects one of eight
  login shapes — classic form, username-first, email-only,
  magic-link, OTP, Microsoft/Azure SSO, generic SSO, unknown — and
  actually performs the login in-process, writing `.auth/user.json`
  for the rest of the run and for subsequent pytest invocations.
  Only inline-form login requires `LOGIN_PASSWORD`; SSO and all
  non-password flows accept `LOGIN_USERNAME` alone and wait for
  interactive MFA completion when run headed.
- Auth-gated shell detection: a page that loads anonymously but
  whose only interactive affordance is a "Sign in with Microsoft"
  button is marked `requires_auth=True` at classify time, so the
  orchestrator never generates tests against the pre-login shell
  of a gated app.
- Homepage reachability probe: when `base_url` is not in the input
  URL list, it is classified once to catch apps whose landing page
  is gated but whose deep-linked inputs happen to render a neutral
  anonymous shell.
- DOM/UI extraction limited to interactive elements; element kind
  honours `<input type=...>` so checkboxes/radios/submits get the
  right Playwright primitive.
- Stable selector discovery with up to four fallbacks per element.
- POM creation/update with selector dictionaries kept in one place.
- BDD feature generation by tier (smoke / sanity / regression / etc).
- Playwright step generation driven by the 3rd LLM prompt
  (`steps_plan`) — one Python statement per unique Gherkin step text,
  AST-validated against POM methods and the SELECTORS catalogue before
  writing. Falls back to deterministic synthesis, then to
  `NotImplementedError` when neither applies.
- **Cross-page awareness** — the 2nd and 3rd prompts receive a
  `known_pages` snapshot of sibling pages (slug + url + POM class)
  so scenarios can bind cross-page nav to real URLs instead of
  guessing at the current page's elements.
- **Runtime self-heal on generated POM actions** — `BasePage.click /
  check / fill / select` auto-unblock disabled targets by ticking
  visible consent checkboxes before retrying. They also **rewrap
  Playwright timeouts into diagnostic AssertionError messages** that
  name the root cause (element not found / hidden / disabled /
  detached) instead of the generic "Timeout 30000ms exceeded".
- Generation quality gate: a URL whose step file still has
  `NotImplementedError` bodies ends up `needs_implementation`, not
  `complete`, and the run summary switches to `run_done_with_issues`.
- **Heal stage** (`autocoder heal`) — fills the stubs via the LLM
  with AST-validated single-statement bodies.
- **Runtime-failure heal** (`autocoder heal --from-pytest` or the
  `AUTOCODER_AUTOHEAL=true` pytest plugin) — captures Playwright
  errors, asks the LLM for revised bodies (up to 5 statements so
  prerequisites are expressible), and patches the step file in place.
- **Failure categorisation** — the HTML report splits failures into
  Frontend (product bug), Script (test-code bug), and Environment
  (flake) so tickets land with the right team.
- **Playwright tracing** — every test's actions are recorded, and
  traces for failing tests are kept under `manifest/traces/` for
  postmortem via `npx playwright show-trace`.
- **Session liveness probe** — auto-auth re-runs when the saved
  `.auth/user.json` is missing *or when its cookies have expired*,
  so stale sessions don't silently fail every test.
- Tracking and resume via `manifest/registry.yaml`. Per-URL
  failures don't abort the whole run — failed URLs are marked and
  the loop continues.
- Rerun awareness: extractions and plans are fingerprinted;
  unchanged pages skip the LLM entirely.
- Coverage extension on existing URLs without duplicating scenarios.
- Local-only verification: `python scripts/verify_local_llm.py`
  records every outbound TCP destination during a real `/api/chat`
  and asserts loopback / private-network only.
