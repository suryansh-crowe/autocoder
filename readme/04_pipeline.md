# 04 · End-to-end pipeline

The orchestrator runs nine stages per `autocoder generate` invocation.
Stages 4 and 6 are the only ones that talk to the LLM. Stage 2 (auth)
now actually performs the login in-process. Everything else is
deterministic.

```
                   you provide URLs
                          │
                          ▼
┌────────────────────────────────────────────────────────────────┐
│ 1. INTAKE      autocoder/intake/                               │
│   classifier.py  open each URL in a real browser (anonymous)   │
│                  goto_resilient: commit → domcontentloaded →   │
│                  networkidle; URL-path hints preserve LOGIN    │
│                  across nav timeouts; redirect/popup capture   │
│                  → public | login | redirect_to_login | auth   │
│   graph.py       build dependency graph; login sorts first     │
│   ─ writes      manifest/registry.yaml: { url → URLNode }      │
│   ─ tokens      0 (browser only)                               │
└────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌────────────────────────────────────────────────────────────────┐
│ 1b. HOMEPAGE PROBE  autocoder/orchestrator.py:_probe_homepage  │
│   ─ runs only when base_url is set and not already an input    │
│   ─ classifies base_url once; if it is login-shaped or redirect│
│     to login, the whole run is marked auth-required even if    │
│     every input URL rendered a neutral shell                   │
│   ─ tokens      0                                              │
└────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌────────────────────────────────────────────────────────────────┐
│ 2. AUTH-FIRST  autocoder/extract/auth_probe.py                 │
│                autocoder/extract/auth_runner.py                │
│                autocoder/generate/auth_setup.py                │
│   ─ fires whenever ANY login signal exists:                    │
│       LOGIN_URL in .env, classifier detection, a LOGIN or      │
│       REDIRECT_TO_LOGIN node, login-shaped path on UNKNOWN,    │
│       or homepage probe positive                               │
│   ─ probes the login page, infers auth_kind:                   │
│       form | username_first | email_only | magic_link |        │
│       otp_code | sso_microsoft | sso_generic | unknown_auth    │
│   ─ renders tests/auth_setup/test_auth_setup.py from the       │
│     template that matches the mode                             │
│   ─ RUNS THE LOGIN in-process (auth_runner), writing           │
│     .auth/user.json on success                                 │
│   ─ on success, stale-marks every non-LOGIN node so the next   │
│     pass re-extracts under the session                         │
│   ─ on awaiting_external_completion, persists any cookies the  │
│     IdP already set, logs a clear hint, and continues          │
│   ─ writes      registry.auth = AuthSpec{...}                  │
│   ─ tokens      0                                              │
└────────────────────────────────────────────────────────────────┘
                          │
                          ▼   for each remaining URL, in dep order
┌────────────────────────────────────────────────────────────────┐
│ 3. EXTRACT     autocoder/extract/inspector.py + selectors.py   │
│   ─ uses storage_state when auth is ready, INCLUDING for       │
│     nodes classified PUBLIC (anonymous classification does not │
│     prove the authenticated DOM is identical)                  │
│   ─ goto_resilient with redirect chain + console errors +      │
│     failed requests captured as diagnostics                    │
│   ─ if goto lands on a login-shaped URL, _maybe_escalate_to_   │
│     auth reclassifies, seeds auth if needed, and retries       │
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
│                     _try_parse_json ladder: direct → fence     │
│                     strip → balanced-brace slice → repair of   │
│                     unterminated strings; one strict-prompt    │
│                     retry before raising OllamaError           │
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
│   ─ validator   each step.pom_method must exist; close-match   │
│                 rebind via difflib before nulling; dedupe      │
│                 scenarios by title                             │
│   ─ fallback    on OllamaError we return a minimal FeaturePlan │
│                 (POM + steps still render) instead of failing  │
│                 the URL                                        │
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
│                   (pytest-bdd; one decorator per unique step;  │
│                    body = pom_method call OR synthesized       │
│                    Playwright call for navigation/assertion/   │
│                    negation patterns OR NotImplementedError    │
│                    when neither fits)                          │
│   ─ quality gate: count NotImplementedError in the rendered    │
│     file. > 0 → node.status = NEEDS_IMPLEMENTATION; the run    │
│     summary surfaces it via run_done_with_issues               │
│   ─ tokens      0                                              │
└────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌────────────────────────────────────────────────────────────────┐
│ 8. PERSIST     autocoder/registry/store.py                     │
│   ─ status      pending → extracted → pom_ready →              │
│                 feature_ready → steps_ready →                  │
│                 (complete | needs_implementation | failed)     │
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
| 1b. Homepage   | `autocoder/orchestrator.py`         | 0                  | Extra login signal when `base_url` is gated |
| 2. Auth-first  | `autocoder/extract/auth_probe.py` + `auth_runner.py` + `generate/auth_setup.py` | 0 | `tests/auth_setup/test_auth_setup.py` + `.auth/user.json` + populated `AuthSpec` |
| 3. Extract     | `autocoder/extract/inspector.py`    | 0                  | `manifest/extractions/<slug>.json` (under session when auth is ready) |
| 4. POM plan    | `autocoder/llm/plans.py`            | ~400 in / ~120 out | `manifest/plans/*.pom.<fp>.json` |
| 5. POM render  | `autocoder/generate/pom.py`         | 0                  | `tests/pages/<slug>_page.py` |
| 6. Feature plan| `autocoder/llm/plans.py`            | ~350 in / ~180 out | `manifest/plans/*.feature.<tiers>.<fp>.json` |
| 7a. Feature    | `autocoder/generate/feature.py`     | 0                  | `tests/features/<slug>.feature` |
| 7b. Steps      | `autocoder/generate/steps.py`       | 0                  | `tests/steps/test_<slug>.py` (+ quality gate) |
| 8. Persist     | `autocoder/registry/`               | 0                  | `registry.yaml` + `manifest/logs/<ts>-<cmd>.log` |
| **9. Heal (optional)** | `autocoder/heal/`           | ~250 in / ~30 out per stub; ~400 / ~80 per failure | Step bodies in `tests/steps/test_<slug>.py` (stub fill or runtime-failure revision) |

## Resume / rerun in one paragraph

A rerun starts at the first incomplete stage for each URL. An
extraction whose fingerprint matches the previous run skips stages
4–7 entirely (zero LLM calls). `autocoder generate --force` ignores
caches and rebuilds every artifact. `autocoder extend --tier ...`
adds new tiers to existing URLs without duplicating scenarios — the
validator dedupes by scenario title. A single URL's exception in
stages 3–8 is caught (status set to `failed`, registry saved) so the
loop continues with the next URL. Nodes that reached stage 7 but
still have placeholder step bodies end up as
`needs_implementation` — not `complete` — so reruns or `autocoder heal`
pick them up again. See `11_manifest.md` for the gory detail and
`17_heal.md` for the heal stage.
