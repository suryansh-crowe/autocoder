# 04 · End-to-end pipeline

There are two entry points:

| Command | What it runs |
|---------|-------------|
| `autocoder generate` | Stages 1-10 only. Produces files on disk. Does **not** invoke pytest. |
| `autocoder run` | Stages 1-10 + pytest + heal-from-pytest + pytest loop, up to `--max-heal-attempts` (default 3) passes. The canonical "give me tests that actually pass" entry point. |

Both share the generation stages below. `autocoder run` adds a
verification phase described at the end of this doc.

The orchestrator runs the **three-prompt per-page sequence** on top
of the classification + auth + extraction phases. Stages 4, 6 and 8
are the LLM calls; everything else is deterministic. Stage 2 (auth)
actually performs the login in-process.

The three LLM calls per page, in order:

1. **Prompt 1 — POM plan** (stage 4): pick method names + actions for
   the extracted controls.
2. **Prompt 2 — Feature plan** (stage 6): translate the control JSON
   into per-control-type Gherkin scenarios across the requested tiers.
3. **Prompt 3 — Playwright codegen** (stage 8): turn the feature plan
   into a pure-Playwright pytest script whose body is a list of
   statements that call the POM fixture or Playwright primitives.

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
│   ─ RUNS THE LOGIN in-process (auth_runner). Password is only  │
│     required for inline-form login; SSO modes accept username- │
│     only and wait up to AUTH_INTERACTIVE_TIMEOUT_MS (default   │
│     300s headed / 90s headless) for interactive MFA completion │
│   ─ writes .auth/user.json ONLY after _wait_success confirms   │
│     the page left login.microsoftonline.com and /login         │
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
│   ─ skips protected nodes entirely when requires_auth=True     │
│     but no storage_state exists yet (logs url_skipped_         │
│     awaiting_auth; marks node needs_implementation)            │
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
│ 6. FEATURE PLAN  autocoder/llm/  (2nd LLM call — Prompt 2)     │
│   ─ input       extraction summary + POM method names + tiers  │
│   ─ tiers       smoke | sanity | regression | happy | edge |   │
│                 validation | navigation | auth | rbac | e2e    │
│   ─ per-control scenarios (textbox → fill + valid/invalid,     │
│     button → click + consequence, checkbox → toggle +          │
│     dependent, form → submit + validation, link/tab → URL nav) │
│   ─ validator   each step.pom_method must exist; close-match   │
│                 rebind via difflib before nulling; dedupe      │
│                 scenarios by title                             │
│   ─ fallback    on OllamaError we return a minimal FeaturePlan │
│                 so the POM still renders and the user can      │
│                 rerun once the LLM is healthy                  │
│   ─ output      FeaturePlan{feature, background, scenarios[]}  │
│   ─ tokens      ~350 in / ~180 out                             │
└────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌────────────────────────────────────────────────────────────────┐
│ 7. RENDER FEATURE FILE    autocoder/generate/feature.py        │
│   feature.py    → tests/features/<slug>.feature                │
│                   (Gherkin, tier→tag mapping; kept as an       │
│                    intermediate artifact the user can review)  │
│   ─ tokens      0                                              │
└────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌────────────────────────────────────────────────────────────────┐
│ 8. PLAYWRIGHT CODEGEN  autocoder/llm/codegen.py (3rd LLM call) │
│   ─ input       FeaturePlan + POMPlan + element catalog        │
│   ─ system      PLAYWRIGHT_CODEGEN_SYSTEM (per-control test    │
│                 rules, pom.locate/expect/page primitives, one  │
│                 statement per list entry, navigate() first)    │
│   ─ validator   generate/playwright_script.py re-checks every  │
│                 statement against the POM method names and the │
│                 SELECTORS catalogue; unsafe statements become  │
│                 NotImplementedError stubs so the heal stage    │
│                 can revise them                                │
│   ─ fallback    on OllamaError a one-statement-per-scenario    │
│                 placeholder plan is used so the file still     │
│                 renders and is picked up by the heal stage     │
│   ─ output      PlaywrightScriptPlan{url, pom_*, tests[]}      │
│   ─ tokens      ~500 in / ~300 out                             │
└────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌────────────────────────────────────────────────────────────────┐
│ 9. RENDER PLAYWRIGHT SCRIPT  autocoder/generate/playwright_    │
│                              script.py                         │
│   ─ writes      tests/playwright/test_<slug>.py                │
│   ─ imports     tests.pages.<slug>_page.<SlugPage> + pytest +  │
│                 playwright.sync_api.expect                     │
│   ─ one pytest test function per scenario, decorated with      │
│     @pytest.mark.<tier> (+ extra tags)                         │
│   ─ quality gate: count NotImplementedError stubs in the       │
│     rendered file. > 0 → node.status = NEEDS_IMPLEMENTATION    │
│   ─ tokens      0                                              │
└────────────────────────────────────────────────────────────────┘
                          │
                          ▼
┌────────────────────────────────────────────────────────────────┐
│ 10. PERSIST    autocoder/registry/store.py                     │
│   ─ status      pending → extracted → pom_ready →              │
│                 feature_ready →                                │
│                 (complete | needs_implementation | failed)     │
│   ─ writes      registry.yaml + manifest/logs/<ts>-<cmd>.log   │
└────────────────────────────────────────────────────────────────┘
                          │
                          ▼
                pure-Playwright pytest suite
                ready to execute
```

## Stage outputs at a glance

| Stage                   | Lives in                            | LLM tokens         | Output |
|-------------------------|-------------------------------------|--------------------|--------|
| 1. Intake               | `autocoder/intake/`                 | 0                  | `URLNode` rows in `registry.yaml` |
| 1b. Homepage probe      | `autocoder/orchestrator.py`         | 0                  | Extra login signal when `base_url` is gated |
| 2. Auth-first           | `autocoder/extract/auth_probe.py` + `auth_runner.py` + `generate/auth_setup.py` | 0 | `tests/auth_setup/test_auth_setup.py` + `.auth/user.json` + populated `AuthSpec` |
| 3. Extract              | `autocoder/extract/inspector.py`    | 0                  | `manifest/extractions/<slug>.json` (the controls JSON consumed by prompts 2 + 3) |
| 4. Prompt 1 — POM plan  | `autocoder/llm/plans.py`            | ~400 in / ~120 out | `manifest/plans/*.pom.<fp>.json` |
| 5. POM render           | `autocoder/generate/pom.py`         | 0                  | `tests/pages/<slug>_page.py` |
| 6. Prompt 2 — Feature plan | `autocoder/llm/plans.py`         | ~350 in / ~180 out | `manifest/plans/*.feature.<tiers>.<fp>.json` |
| 7. Feature render       | `autocoder/generate/feature.py`     | 0                  | `tests/features/<slug>.feature` |
| 8. Prompt 3 — Playwright codegen | `autocoder/llm/codegen.py` | ~500 in / ~300 out | `manifest/plans/*.playwright.<fp>.<featfp>.json` |
| 9. Playwright render    | `autocoder/generate/playwright_script.py` | 0            | `tests/playwright/test_<slug>.py` (+ quality gate) |
| 10. Persist             | `autocoder/registry/`               | 0                  | `registry.yaml` + `manifest/logs/<ts>-<cmd>.log` |
| **11. Heal (optional)** | `autocoder/heal/`                   | ~300 / ~80 per stub or failure | Test function bodies in `tests/playwright/test_<slug>.py` (stub fill or runtime-failure revision at the function level) |

## Verification + heal loop (`autocoder run` only)

After stage 8 completes for every URL, `autocoder run` enters a
verification phase. This phase is where runtime failures — bad
locators, wrong control-type assumptions (`.check()` on a button),
timing issues, DOM drift, assertion mismatches — get healed.

```
                    for every slug with status in (complete, needs_implementation)
                    and a tests/playwright/test_<slug>.py file on disk:
                             │
                             ▼
┌────────────────────────────────────────────────────────────────┐
│ 11. VERIFY     autocoder/orchestrator.py:_run_pytest_for_slug  │
│   ─ invokes `pytest tests/playwright/test_<slug>.py` with a    │
│     JUnit report at manifest/runs/<slug>.xml                   │
│   ─ parses failures via heal/pytest_failures.py                │
│   ─ captures failure_class (timeout, disabled, intercepted,    │
│     wrong_kind, locator_not_found, not_visible, not_attached,  │
│     other) for each failing test                               │
│   ─ emits pytest_outcome slug=X passed=T/F failures=N          │
└────────────────────────────────────────────────────────────────┘
                             │
                 all passed? ┤
                     yes ◄───┘───► no, and heal_attempts < max_heal_attempts
                                                    │
                                                    ▼
┌────────────────────────────────────────────────────────────────┐
│ 12. HEAL       autocoder/heal/runner.py  (from_pytest=True)    │
│   ─ per failing test: read the JUnit XML, build a FAILURE_HEAL │
│     prompt with failure_class + scenario title + current body  │
│     + Playwright error message                                 │
│   ─ ONE LLM call per failing test function, json_mode=True     │
│   ─ validator enforces: ≤20 stmts, allowed node types,         │
│     every fixture method must exist in the POM plan,           │
│     every `locate('id')` must reference a real SELECTORS key   │
│   ─ applier replaces the entire function body and re-parses    │
│     the file; rolls back if the rewrite no longer parses       │
│   ─ suggestions cached by (slug, function, fingerprint,        │
│     failure_class) so reruns of the same failure cost 0 tokens │
│   ─ emits run_heal_slug_done slug=X attempt=N applied=K        │
│   if no body changed this attempt → exit loop early            │
└────────────────────────────────────────────────────────────────┘
                             │
                             ▼
                back to stage 9 for the same failing slugs
```

End-of-cycle bookkeeping:

- Slugs whose tests pass → `Status.VERIFIED` in `registry.yaml`.
- Slugs that failed at least one test after the heal budget was spent
  → `Status.NEEDS_IMPLEMENTATION` with `heal_attempts=N` and
  `last_pytest_outcome="fail"`.
- Every URL gains `last_verified_at` ISO timestamp.

The CLI exit code is **1** when any URL is still failing, **0** when
everything verified.

Default `--max-heal-attempts=3` is tuned for a local LLM (`phi4:14b`
on CPU): high enough to recover from the common failure modes
(`locator_not_found`, `wrong_kind`, `disabled`, `timeout`,
`intercepted`, `not_visible`) without blowing the wall-clock budget.
Override it on the CLI or set `0` to skip healing entirely.

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
