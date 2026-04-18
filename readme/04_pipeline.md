# 04 · End-to-end pipeline

The orchestrator runs eight stages per `autocoder generate` invocation.
Stages 4 and 6 are the only ones that talk to the LLM. Everything else
is deterministic.

```
                   you provide URLs
                          │
                          ▼
┌────────────────────────────────────────────────────────────────┐
│ 1. INTAKE      autocoder/intake/                               │
│   classifier.py  open each URL in a real browser (anonymous)   │
│                  → public | login | redirect_to_login | auth   │
│   graph.py       build dependency graph; login sorts first     │
│   ─ writes      manifest/registry.yaml: { url → URLNode }      │
│   ─ tokens      0 (browser only)                               │
└────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌────────────────────────────────────────────────────────────────┐
│ 2. AUTH-FIRST  autocoder/extract/auth_probe.py                 │
│                autocoder/generate/auth_setup.py                │
│   ─ runs only when any URL is authenticated                    │
│   ─ probes the login page, picks stable selectors for          │
│     username / password / submit                               │
│   ─ renders tests/auth_setup/test_auth_setup.py                │
│     (reads creds from env; never embeds secrets)               │
│   ─ writes      registry.auth = AuthSpec{...}                  │
│   ─ tokens      0 (deterministic template)                     │
└────────────────────────────────────────────────────────────────┘
                          │
                          ▼   for each remaining URL, in dep order
┌────────────────────────────────────────────────────────────────┐
│ 3. EXTRACT     autocoder/extract/inspector.py + selectors.py   │
│   ─ opens the URL with storage_state if it needs auth          │
│   ─ enumerates interactive elements only (cap: 60/page)        │
│   ─ picks one stable selector + 4 fallbacks per element        │
│     priority: test_id > role+name > label > placeholder >      │
│               text > css > xpath                               │
│   ─ writes      manifest/extractions/<slug>.json               │
│   ─ fingerprint = hash(elements + headings + forms + title)    │
│   ─ tokens      0 (browser only)                               │
└────────────────────────────────────────────────────────────────┘
                          │
       ┌──────────────────┴──────────────────┐
       │  fingerprint == last_fingerprint?    │
       │     yes → skip stages 4–7            │
       │     no  → continue                   │
       └──────────────────┬──────────────────┘
                          ▼
┌────────────────────────────────────────────────────────────────┐
│ 4. POM PLAN    autocoder/llm/  (1st of 2 LLM calls)            │
│   ollama_client.py  POST /api/chat to phi4:14b, JSON mode      │
│   prompts.py        compact prompt: only id+role+name+kind     │
│   validator.py      reject methods that reference unknown ids  │
│   plans.py          cache result keyed by fingerprint          │
│   ─ output     POMPlan{class_name, fixture_name, methods[]}    │
│   ─ tokens     ~400 in / ~120 out                              │
└────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌────────────────────────────────────────────────────────────────┐
│ 5. POM RENDER  autocoder/generate/pom.py                       │
│   ─ writes      tests/pages/<slug>_page.py                     │
│   ─ extends     tests/pages/base_page.py                       │
│   ─ contains    SELECTORS = {<id>: [primary, ...fallbacks]}    │
│                 + one method per POMMethod (deterministic)     │
│   ─ tokens      0                                              │
└────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌────────────────────────────────────────────────────────────────┐
│ 6. FEATURE PLAN  autocoder/llm/  (2nd of 2 LLM calls)          │
│   ─ input       extraction summary + POM method names + tiers  │
│   ─ tiers       smoke | sanity | regression | happy | edge |   │
│                 validation | navigation | auth | rbac | e2e    │
│   ─ validator   each step.pom_method must exist; dedupe titles │
│   ─ output      FeaturePlan{feature, background, scenarios[]}  │
│   ─ tokens      ~350 in / ~180 out                             │
└────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌────────────────────────────────────────────────────────────────┐
│ 7. RENDER FEATURE + STEPS    autocoder/generate/{feature,steps}│
│   feature.py    → tests/features/<slug>.feature                │
│                   (Gherkin, tier→tag mapping)                  │
│   steps.py      → tests/steps/test_<slug>.py                   │
│                   (pytest-bdd; one decorator per unique step,  │
│                    body calls fixture.<pom_method>(*args))     │
│   ─ tokens      0                                              │
└────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌────────────────────────────────────────────────────────────────┐
│ 8. PERSIST     autocoder/registry/store.py                     │
│   ─ status      pending → extracted → pom_ready →              │
│                 feature_ready → steps_ready → complete         │
│   ─ writes      registry.yaml + manifest/logs/<ts>-<cmd>.log   │
└────────────────────────────────────────────────────────────────┘
                          │
                          ▼
                   pytest-bdd suite
              ready to execute
```

## Stage outputs at a glance

| Stage          | Lives in                            | LLM tokens         | Output |
|----------------|-------------------------------------|--------------------|--------|
| 1. Intake      | `autocoder/intake/`                 | 0                  | `URLNode` rows in `registry.yaml` |
| 2. Auth-first  | `autocoder/extract/auth_probe.py`   | 0                  | `tests/auth_setup/test_auth_setup.py` + `AuthSpec` |
| 3. Extract     | `autocoder/extract/inspector.py`    | 0                  | `manifest/extractions/<slug>.json` |
| 4. POM plan    | `autocoder/llm/plans.py`            | ~400 in / ~120 out | `manifest/plans/*.pom.<fp>.json` |
| 5. POM render  | `autocoder/generate/pom.py`         | 0                  | `tests/pages/<slug>_page.py` |
| 6. Feature plan| `autocoder/llm/plans.py`            | ~350 in / ~180 out | `manifest/plans/*.feature.<tiers>.<fp>.json` |
| 7a. Feature    | `autocoder/generate/feature.py`     | 0                  | `tests/features/<slug>.feature` |
| 7b. Steps      | `autocoder/generate/steps.py`       | 0                  | `tests/steps/test_<slug>.py` |
| 8. Persist     | `autocoder/registry/`               | 0                  | `registry.yaml` + `manifest/logs/<ts>-<cmd>.log` |
| **9. Heal (optional)** | `autocoder/heal/`           | ~250 in / ~30 out per stub; ~400 / ~80 per failure | Step bodies in `tests/steps/test_<slug>.py` (stub fill or runtime-failure revision) |

## Resume / rerun in one paragraph

A rerun starts at the first incomplete stage for each URL. An
extraction whose fingerprint matches the previous run skips stages 4–7
entirely (zero LLM calls). `autocoder generate --force` ignores caches
and rebuilds every artifact. `autocoder extend --tier ...` adds new
tiers to existing URLs without duplicating scenarios — the validator
dedupes by scenario title. A single URL's exception in stages 3–8
is caught (status set to `failed`, registry saved) so the loop
continues with the next URL. See `11_manifest.md` for the gory
detail and `17_heal.md` for the heal stage.
