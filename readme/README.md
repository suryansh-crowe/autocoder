# autocoder — documentation index

URL-driven Playwright BDD test automation built around a local Phi-4 14B
LLM. The orchestrator turns a list of URLs into a runnable pytest-bdd
suite — POMs, features, step definitions, and an auth-setup test —
while keeping the LLM's role to a single tiny JSON action plan per
stage.

**Start here:** `autocoder run <urls>` is the single command most
users want. It generates the suite, runs pytest, invokes
`heal --from-pytest` for anything that fails at runtime, and loops
up to `--max-heal-attempts` passes (default 3) before reporting a
final per-URL state of `verified` / `needs_implementation` / `failed`.
Exits non-zero when any URL is still failing, so CI catches it.

`autocoder generate` and the separate `autocoder heal` commands
remain available for offline / fine-grained workflows.

## Reading order

If you are new, read top-to-bottom. Each document is short enough to
finish in a few minutes.

| # | Doc | Purpose |
|---|-----|---------|
| 01 | [overview.md](01_overview.md) | What the system is, what it is not, design principles |
| 02 | [quickstart.md](02_quickstart.md) | Install, env, first generation, first test run |
| 03 | [architecture.md](03_architecture.md) | Package layout + component responsibilities |
| 04 | [pipeline.md](04_pipeline.md) | The end-to-end flow diagram, stage by stage |
| 05 | [url_intake.md](05_url_intake.md) | URL sources, classification, dependency graph |
| 06 | [auth_first.md](06_auth_first.md) | Auth-first handling, secret rules, storage_state |
| 07 | [extraction.md](07_extraction.md) | Browser inspection + page fingerprint |
| 08 | [selectors_and_self_healing.md](08_selectors_and_self_healing.md) | Selector priority, runtime self-heal |
| 09 | [llm.md](09_llm.md) | Ollama / Phi-4 client, prompts, plan validation, cache |
| 10 | [generation.md](10_generation.md) | Deterministic renderers (POM / feature / steps / auth) |
| 11 | [manifest.md](11_manifest.md) | Registry, runs log, plans + heals caches, resume, rerun |
| 12 | [running_tests.md](12_running_tests.md) | Running the generated suite (pytest, tiers, markers) |
| 13 | [token_budget.md](13_token_budget.md) | Token cost per stage + why it stays low |
| 14 | [extending.md](14_extending.md) | Adding selector strategies, tiers, or models |
| 15 | [logging.md](15_logging.md) | Log levels, token accounting, decision events, redaction rules |
| 17 | [heal.md](17_heal.md) | `autocoder heal` — fill stubs + heal runtime test failures |
| 18 | [cleanup.md](18_cleanup.md) | Start from zero — one-shot commands to wipe generated artifacts + auth session |

## Run reports

End-to-end execution records, each pinned to a specific date and
codebase state so the findings stay interpretable over time.

| Date | Report |
|------|--------|
| 2026-04-18 | [16_run_report_2026-04-18.md](16_run_report_2026-04-18.md) — first end-to-end run; 3 attempts; 5 bugs found and fixed; both URLs reached `complete`; 4 048 tokens spent. |

## Source-of-truth ordering

When something disagrees, trust in this order:

1. Live behavior of the URLs you provide (the browser observes truth).
2. Your CLI flags and `.env` configuration.
3. The persisted `manifest/registry.yaml` from prior runs.
