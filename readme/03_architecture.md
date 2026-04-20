# 03 · Architecture

The repository is split into three independent halves:

- **`src/autocoder/`** — the orchestration system (Python package). It
  does the work of producing tests. Importable as `autocoder`.
- **`tests/`** — the Playwright BDD suite the orchestrator produces +
  hand-written scaffolding (base POM, locator resolver, fixtures).
- **`manifest/`** — the persistent on-disk state that links them:
  every URL the orchestrator has touched, every extraction
  fingerprint, every cached LLM plan. **This is the only `manifest`
  in the project — it holds runtime data, not code.** The Python
  code that reads/writes it lives in `src/autocoder/registry/`.

## Directory layout

```
autocoder/                        Project root (= the "autocoder" project)
  pyproject.toml                  Package + pytest + ruff config (src layout)
  pytest.ini                      Markers + bdd_features_base_dir
  requirements.txt                Pinned direct dependencies
  conftest.py                     Top-level pytest config (.env loader)
  .env.example                    Template for the local `.env`

  src/                            Python source (src layout)
    autocoder/                    The installable package, imported as `autocoder`
      __init__.py                 Public exports
      config.py                   Settings dataclass, .env loader, path layout
      models.py                   Pydantic models shared across stages
      utils.py                    slugify, fingerprint, identifier helpers
      logger.py                   Console + JSON-line file logger
      cli.py                      `autocoder` CLI (generate / run / extend / heal / rerun / status / report)
      orchestrator.py             End-to-end pipeline that ties stages together;
                                   runs inline `steps_autoheal` + pre-write syntax guard
      report.py                   Coverage + execution report: per-URL component chips,
                                   per-scenario pass/fail, HTML / JSON / rich-table output

      intake/                     Stage 1: classify URLs
        classifier.py               Real-browser probe → URLNode (uses goto_resilient)
        graph.py                    Dependency graph + topological order
        sources.py                  4-tier URL resolution (CLI > file > env > settings)

      extract/                    Stage 1b/2/3: navigation + UI catalog + auth
        browser.py                  Playwright session + goto_resilient + AuthUnreachable
        inspector.py                Compact element catalog per page
        selectors.py                Stable-selector resolver (priority order)
        auth_probe.py               Infer auth_kind (8 modes); detect form / SSO / magic-link / OTP / username-first / email-only
        auth_runner.py              In-process login runner: dispatches by auth_kind, drives Entra for SSO, writes storage_state

      llm/                        Stages 4 + 6: single LLM call per stage
        ollama_client.py            HTTP client tuned for CPU-only Phi-4
        azure_client.py             Azure OpenAI client (drop-in replacement)
        factory.py                  Picks backend from USE_AZURE_OPENAI
        prompts.py                  Compact prompts + build_ui_inventory() — categorises
                                    page components (search/chat/nav/forms/data/
                                    pagination/buttons/choices) for the feature plan
        plans.py                    High-level helpers + on-disk plan cache
        validator.py                Grammar validation against the catalog

      generate/                   Stages 5 + 7: deterministic renderers
        pom.py                      Render tests/pages/<slug>_page.py
        feature.py                  Render tests/features/<slug>.feature
        steps.py                    Render tests/steps/test_<slug>.py
        auth_setup.py               Render tests/auth_setup/test_auth_setup.py

      heal/                       Optional stage 9: LLM-backed step healing
        scanner.py                  Find renderer-shaped NotImplementedError stubs
        prompts.py                  Stub-heal + failure-heal prompts. Carries
                                    forbidden_element_ids (ids prior scenario steps
                                    already acted on) + current_page_url for the
                                    trivial-URL rule
        validator.py                AST-validate suggestions (1 stmt for stubs, ≤5 for
                                    failures); rejects trivial `to_have_url(page_url)`
                                    and forbidden-id locates
        applier.py                  Line-replace; re-parse to roll back on broken output
        pytest_failures.py          Run pytest, parse JUnit XML, classify failures
        runner.py                   Orchestrate heal: scan, LLM, validate, apply, cache.
                                    `_scenario_prior_step_texts` + `_compute_forbidden_ids`
                                    parse the .feature file to pass scenario context
                                    into the heal prompt per stub

      registry/                   Stage 8: persistence + change detection
        store.py                    Read/write manifest/registry.yaml
        diff.py                     Detect element/selector changes per URL
        resume.py                   Decide which nodes still need work

  tests/                          The Playwright BDD suite (generated + scaffold)
    conftest.py                     Shared fixtures (storage_state aware)
    features/                       Generated *.feature files
    steps/                          Generated step-definition modules (test_*.py)
    pages/
      base_page.py                  Hand-written base POM (uses self-healing locator)
      <slug>_page.py                Generated POMs
    support/
      locator_strategy.py           Runtime self-healing resolver
      env.py                        Safe env-var helpers
    auth_setup/
      test_auth_setup.py            Generated auth setup (writes storage_state)
    .auth/                          storage_state.json — gitignored

  manifest/                       Runtime data (created on first run; gitignored)
    registry.yaml                   Source of truth: URL → URLNode + AuthSpec
    extractions/                    Per-URL compact extraction snapshots (JSON)
    plans/                          Cached LLM POM/feature plans (skip on rerun)
    heals/                          Cached heal suggestions (stub + failure)
    runs/<slug>.xml                 Per-slug JUnit XML (split from a pytest session
                                    by tests/conftest.py:pytest_sessionfinish)
    runs/_pytest_session.xml        Merged JUnit XML from the last pytest run
                                    (input to the auto-split above)
    report.html                     Generated by `autocoder report --html` and by
                                    the auto-report pytest hook (opt out with
                                    AUTOCODER_AUTOREPORT=false)
    logs/<YYYYMMDD-HHMMSS>-<cmd>.log Per-invocation newline-delimited JSON run log

  readme/                         This documentation set
  scripts/
    verify_local_llm.py             Live test: prove inference is local-only
```

## On the two halves with similar names

| Name | What it is | Where |
|------|------------|-------|
| `src/autocoder/registry/` | Python subpackage. Defines `RegistryStore`, diff, resume helpers. | Code |
| `manifest/` | On-disk runtime data directory. Holds `registry.yaml`, extractions, plans, logs. | Data |

The two used to share the name `manifest`, which was confusing. The
code half is now `registry`; the data folder keeps `manifest` because
that is the user-facing path in `.env` and CLI output.

## Component responsibilities

| Concern | Lives in | Why there |
|---------|----------|-----------|
| Reading env / file paths | `autocoder/config.py` | One place to change defaults; tests can pass an in-memory `Settings`. |
| URL classification | `autocoder/intake/classifier.py` | Needs a real browser; produces `URLNode`s for the registry. |
| Dependency ordering | `autocoder/intake/graph.py` | Pure function over `URLNode`s; deterministic. |
| Resilient navigation | `autocoder/extract/browser.py:goto_resilient` | Tiered `commit`/`domcontentloaded`/`networkidle` strategy + diagnostics dump on timeout. Reused by classifier, auth probe, auth runner, and extract. |
| Auth-mode inference | `autocoder/extract/auth_probe.py` | Picks one of 8 `auth_kind` values from page shape (form / SSO / username-first / email-only / magic-link / OTP / unknown). |
| Auth-gated shell detection | `autocoder/intake/classifier.py:_looks_auth_gated` | Flags anonymously-reachable pages whose only interactive affordance is an SSO / sign-in button; sets `requires_auth=True` without changing `kind`. |
| In-process login | `autocoder/extract/auth_runner.py` | Actually performs the login during `autocoder generate` and writes `.auth/user.json`. Mode-aware credential gating — password only required for `form`. SSO modes wait up to `AUTH_INTERACTIVE_TIMEOUT_MS` (default 45 s) for interactive MFA. `_unblock_sso_button` polls consent checkboxes for up to 15 s (handles reactive SPAs), click failures fall through to `_wait_success` instead of aborting, and `_wait_success` accepts three signals: URL-based, MSAL tokens in sessionStorage/localStorage, and a proactive nav to `base_url` when the browser is stuck on a 404 `/login` route. |
| Homepage probe | `autocoder/orchestrator.py:_probe_homepage` | Catches apps whose base URL is gated but no input URL happened to be. |
| Stable selectors | `autocoder/extract/selectors.py` | The generation-time half of the locator strategy. |
| Self-healing locators | `tests/support/locator_strategy.py` | The runtime half; lives with the suite so generated POMs need no extra imports. |
| LLM call | `autocoder/llm/ollama_client.py` | Only place that talks to Ollama. Includes `_try_parse_json` recovery ladder + one strict-prompt retry. |
| Plan caching | `autocoder/llm/plans.py` | Keyed by extraction fingerprint; reruns of unchanged pages cost zero tokens. `generate_feature_plan` falls back to a minimal plan on `OllamaError` so POM + steps still render. |
| Plan validation | `autocoder/llm/validator.py` | Drops unknown element ids; close-match rebind via difflib for `pom_method` misses. |
| Code generation | `autocoder/generate/*.py` | Templates only; never invokes the LLM. Includes step synthesis for navigation / assertion / negation patterns. |
| Auth-setup templates | `autocoder/generate/auth_setup.py` | Four templates dispatched by `auth_kind` (form, SSO, username-first, email-only / magic / OTP). |
| Persistence | `autocoder/registry/store.py` | Single Python entry point for `manifest/registry.yaml`. |
| Stub healing | `autocoder/heal/runner.py` | Fills `NotImplementedError` stubs the renderer left. |
| Failure healing | `autocoder/heal/runner.py` + `pytest_failures.py` | Runs pytest, parses JUnit XML, asks the LLM for revised step bodies. |
| URL source resolution | `autocoder/intake/sources.py` | 4-tier fallback (CLI > file > env > settings); structure-aware splitting. |
| Local-only verification | `scripts/verify_local_llm.py` | Records every outbound TCP destination during a live `/api/chat`. |
| Pipeline glue | `autocoder/orchestrator.py` | The only module that knows about all stages; per-URL exceptions are caught so one failure doesn't abort the run. Runs inline `steps_autoheal` after rendering, calls `ast.parse()` on every rendered step module and falls back to `_strip_step_bodies_for_heal` (rewriting bad bodies to `NotImplementedError` stubs) if the output is invalid Python. Also owns the placeholder quality gate + `run_done_with_issues` summary. |
| Coverage + execution report | `autocoder/report.py` | Builds `ReportData` from the registry + extractions + JUnit XML; renders rich-table, JSON, or standalone HTML. Invoked by `autocoder report` and by the `pytest_sessionfinish` hook in `tests/conftest.py`. |
| User-facing commands | `autocoder/cli.py` | Thin layer over `orchestrator.py` + `heal/runner.py` + `report.py`. |
