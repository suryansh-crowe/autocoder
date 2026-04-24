# autocoder — documentation index

URL-driven Playwright BDD test automation. Give it a list of URLs and
an LLM backend (local Phi-4 via Ollama, or Azure OpenAI); get back a
runnable pytest-bdd suite — page objects, feature files, step
definitions, and an auth-setup test — with self-healing baked in at
both generation and runtime.

## The 3-prompt per-page flow

For every URL you hand it, the agent runs three LLM calls back-to-back:

1. **`pom_plan`** — controls JSON → Page Object method plan.
2. **`feature_plan`** — controls JSON + POM → Gherkin scenarios with
   per-control-type test cases.
3. **`steps_plan`** — feature plan + POM → a Playwright body for
   every unique Gherkin step.

The system prompts themselves live as JSON files under
`src/autocoder/prompts/` so they can be edited without touching
Python. See `19_prompts.md`.

## Start here

```bash
autocoder run --urls-file urls.txt       # generate + write artifacts
pytest tests/steps                       # run the generated suite
```

With `AUTOCODER_AUTOHEAL=true` in `.env`, any step that fails at
runtime is rewritten in place by the LLM and retried. Any test that
fails leaves a Playwright trace under `manifest/traces/<…>.zip`.

Per-URL generation state ends up as `verified` / `needs_implementation`
/ `failed` in the registry (`manifest/registry.yaml`, gitignored).
Exits non-zero when any URL is still failing so CI catches it.

`autocoder generate` (generation only, no pytest) and `autocoder heal`
(offline re-heal from a prior JUnit XML) remain available for
fine-grained workflows.

## Reading order

Top-to-bottom if you are new. Each doc is short.

| # | Doc | Purpose |
|---|-----|---------|
| 01 | [overview.md](01_overview.md) | What the system is, what it is not, design principles |
| 02 | [quickstart.md](02_quickstart.md) | Install, env, first generation, first test run |
| 03 | [architecture.md](03_architecture.md) | Package layout + component responsibilities |
| 04 | [pipeline.md](04_pipeline.md) | The end-to-end flow diagram, stage by stage |
| 05 | [url_intake.md](05_url_intake.md) | URL sources, classification, dependency graph |
| 06 | [auth_first.md](06_auth_first.md) | Auth-first handling, secret rules, storage_state |
| 07 | [extraction.md](07_extraction.md) | Browser inspection + page fingerprint |
| 08 | [selectors_and_self_healing.md](08_selectors_and_self_healing.md) | Selector priority, runtime self-heal, diagnostic error wrapping |
| 09 | [llm.md](09_llm.md) | Ollama / Azure OpenAI client, JSON-backed prompts, plan cache |
| 10 | [generation.md](10_generation.md) | Deterministic renderers (POM / feature / steps / auth) |
| 11 | [manifest.md](11_manifest.md) | Registry, traces, plans + heals caches, resume |
| 12 | [running_tests.md](12_running_tests.md) | Running the generated suite (pytest, tiers, markers, tracing) |
| 13 | [token_budget.md](13_token_budget.md) | Token cost per stage + why it stays low |
| 14 | [extending.md](14_extending.md) | Adding selector strategies, tiers, or models |
| 15 | [logging.md](15_logging.md) | Log levels, token accounting, decision events, redaction rules |
| 17 | [heal.md](17_heal.md) | `autocoder heal` + autoheal plugin; failure categorisation in the report |
| 18 | [cleanup.md](18_cleanup.md) | Start from zero — one-shot commands to wipe generated artifacts + auth session |
| 19 | [prompts.md](19_prompts.md) | The JSON-backed prompt library — editing prompts without editing Python |

## Source-of-truth ordering

When something disagrees, trust in this order:

1. Live behavior of the URLs you provide (the browser observes truth).
2. Your CLI flags and `.env` configuration (all env values read via `tests/settings.py`).
3. The persisted `manifest/registry.yaml` from prior runs.
