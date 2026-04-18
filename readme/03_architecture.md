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
      cli.py                      `autocoder` CLI (generate / extend / rerun / status)
      orchestrator.py             End-to-end pipeline that ties stages together

      intake/                     Stage 1: classify URLs
        classifier.py               Real-browser probe → URLNode
        graph.py                    Dependency graph + topological order

      extract/                    Stage 3 (and stage-2 probe): UI catalog
        browser.py                  Playwright session context manager
        inspector.py                Compact element catalog per page
        selectors.py                Stable-selector resolver (priority order)
        auth_probe.py               Detect login form fields for auth setup

      llm/                        Stages 4 + 6: single LLM call per stage
        ollama_client.py            HTTP client tuned for CPU-only Phi-4
        prompts.py                  Compact prompts (POM plan, feature plan)
        plans.py                    High-level helpers + on-disk plan cache
        validator.py                Grammar validation against the catalog

      generate/                   Stages 5 + 7: deterministic renderers
        pom.py                      Render tests/pages/<slug>_page.py
        feature.py                  Render tests/features/<slug>.feature
        steps.py                    Render tests/steps/test_<slug>.py
        auth_setup.py               Render tests/auth_setup/test_auth_setup.py

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
    plans/                          Cached LLM JSON plans (skip on rerun)
    runs.log                        Newline-delimited JSON run log

  readme/                         This documentation set
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
| Stable selectors | `autocoder/extract/selectors.py` | The generation-time half of the locator strategy. |
| Self-healing locators | `tests/support/locator_strategy.py` | The runtime half; lives with the suite so generated POMs need no extra imports. |
| LLM call | `autocoder/llm/ollama_client.py` | Only place that talks to Ollama. |
| Plan caching | `autocoder/llm/plans.py` | Keyed by extraction fingerprint; reruns of unchanged pages cost zero tokens. |
| Code generation | `autocoder/generate/*.py` | Templates only; never invokes the LLM. |
| Persistence | `autocoder/registry/store.py` | Single Python entry point for `manifest/registry.yaml`. |
| Pipeline glue | `autocoder/orchestrator.py` | The only module that knows about all stages. |
| User-facing commands | `autocoder/cli.py` | Thin layer over `orchestrator.py`. |
