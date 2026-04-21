# 11 · Manifest, resume, rerun, extension

The **manifest** is the on-disk runtime data folder. It is the
system's memory: what URLs exist, what stage each one reached, the
last extracted state. Resume, rerun, and extension all read from
these files.

The Python code that reads/writes the manifest lives at
`src/autocoder/registry/`. The folder name is `registry` (not
`manifest`) so it does not collide with the data folder.

**Where it lives.** There is no longer a root-level `manifest/`
directory. Every `autocoder generate` invocation scopes its manifest
paths at
`tests/generated/generated_<YYYYMMDD_HHMMSS>/manifest/`, so the run
folder is its own self-contained manifest root. `autocoder heal` and
`autocoder report` rescope to the most recent run's manifest via
`scope_settings_to_latest_run()` so reads/writes target the right
folder without the caller having to specify a path.

**Cross-run cache reuse.** At the start of every `run_generate`,
`seed_manifest_from_previous_run()` copies the newest prior run's
`manifest/` into the new run folder. Fingerprint-based cache hits
keep skipping LLM calls for unchanged slugs — the seed carries the
cached plans, heals, and extractions forward so reruns stay cheap.
Only when there is no prior run (clean clone) does a run start cold.

**Version-control scope.** Nothing under `tests/generated/generated_*/`
is tracked. Every file is reproducible from the previous run's seed
plus a fresh `autocoder generate`.

## Files

```
tests/generated/generated_<YYYYMMDD_HHMMSS>/
  <slug>/                            one per URL processed this run
    <slug>.feature
    test_<slug>.py
    <slug>_page.py
    results.xml                      per-slug JUnit (written by the runner)
  manifest/
    registry.yaml                    single source of truth (URL → URLNode + AuthSpec)
    extractions/<slug>.json
    extractions/<slug>.prev.json     previous run's extraction (for diffs)
    plans/<fixture>.pom.<fp>.json
    plans/<fixture>.feature.<tier_set>.<fp>.json
    heals/<slug>.<key>.json           cached stub-heal suggestions
    heals/<slug>.fail.<key>.json      cached failure-heal suggestions
    heals/last-pytest.xml             JUnit XML from `heal --from-pytest`
    runs/<slug>.xml                   legacy per-slug JUnit — only written when the
                                       flat-layout fallback kicks in
    runs/_pytest_session.xml          legacy merged JUnit (input to the per-slug
                                       split in `tests/conftest.py`)
    report.html                       Standalone HTML dashboard — component chips
                                       per URL, per-scenario pass/fail, totals.
                                       Written by `autocoder report --html` and by
                                       the pytest auto-report hook
    logs/<YYYYMMDD-HHMMSS>-<cmd>.log  per-invocation newline-delimited JSON log
                                       (one file per `autocoder ...` command)
```

`registry.yaml` is the only file you should edit by hand. The others
are derived data and will be overwritten freely.

## Lifecycle of a run folder

Every `run_generate` follows the same three-step setup:

1. **Stamp** — compute ``run_stamp = YYYYMMDD_HHMMSS`` and create
   ``tests/generated/generated_<stamp>/`` with an ``__init__.py``.
2. **Seed** — ``seed_manifest_from_previous_run(settings, run_stamp)``
   finds the newest prior run folder and copies its ``manifest/``
   into the new run's ``manifest/``. This preserves fingerprint-based
   cache hits: unchanged slugs still skip the LLM. When no prior run
   exists (fresh clone) the function returns ``False`` and the run
   starts cold.
3. **Rescope** — ``scope_settings_to_run(settings, run_stamp)`` returns
   a new ``Settings`` whose ``manifest_dir`` /
   ``extractions_dir`` / ``plans_dir`` / ``logs_dir`` /
   ``registry_path`` all live inside the run folder's ``manifest/``.
   From that point every downstream write lands inside the run
   folder.

`autocoder heal` and `autocoder report` use the read-side companion,
``scope_settings_to_latest_run(settings)``, which resolves the
manifest to the newest run folder that contains one. Heal mutates
that run folder in place — the run stays self-contained after
healing.

Properties:

- **Self-contained** — combined with the generated feature / test /
  page and the ``manifest/`` subfolder, each run folder is everything
  needed to reproduce, re-test, or heal that specific invocation.
- **Per-run isolation** — nothing under the run folder is shared
  mutably with any other run. Copy a run folder to another machine
  and it still works (same `.env` / `.auth/` assumed).
- **Cheap reruns** — the seed step preserves cache reuse so a fresh
  run against the same URLs costs zero extra LLM tokens for
  unchanged slugs.
- **History as audit trail** — older run folders stay on disk for
  post-mortems; `tests/generated/conftest.py` filters pytest
  collection to the newest bundle per slug so historical folders
  never re-execute.

## URLNode and AuthSpec

```yaml
nodes:
  https://app.example.com/dashboard:
    url: https://app.example.com/dashboard
    slug: dashboard
    kind: redirect_to_login
    requires_auth: true
    redirects_to: https://app.example.com/login
    depends_on: [https://app.example.com/login]
    status: complete
    extraction_path: manifest/extractions/dashboard.json
    plan_path: manifest/plans/dashboard_page.pom.9b2f3c4d5e6f7081.json
    pom_path: tests/generated/generated_20260421_143022/dashboard/dashboard_page.py
    feature_path: tests/generated/generated_20260421_143022/dashboard/dashboard.feature
    steps_path: tests/generated/generated_20260421_143022/dashboard/test_dashboard.py
    last_fingerprint: 9b2f3c4d5e6f7081
    last_run_at: '2026-04-19T18:42:01+00:00'
auth:
  login_url: https://app.example.com/login
  auth_kind: sso_microsoft                 # form | username_first | email_only |
                                           # magic_link | otp_code | sso_microsoft |
                                           # sso_generic | unknown_auth
  requires_external_completion: false
  username_env: LOGIN_USERNAME
  password_env: LOGIN_PASSWORD
  username_selector: {strategy: css, value: 'input[type=email]'}
  password_selector: null                  # populated only for form + later stages of SSO
  submit_selector: null
  continue_selector: null                  # Next / Continue / Send link / Send code
  sso_button_selector: {strategy: role_name, value: button, name: 'Sign in with Microsoft'}
  success_indicator_url_contains: https://app.example.com
  setup_path: tests/auth_setup/test_auth_setup.py
  storage_state_path: .auth/user.json
  notes: []                                # runner / probe diagnostics
  status: steps_ready
```

## Status lifecycle

A node walks one direction through these statuses, never backwards
(except via `--force`):

```
pending → extracted → pom_ready → feature_ready → steps_ready
                                                     │
                                  ┌──────────────────┼─────────────────────┐
                                  ▼                  ▼                     ▼
                               complete    needs_implementation           failed
```

The orchestrator advances the status **after** writing each artifact,
so a crash mid-write leaves the prior status intact.

- **`complete`** — all artifacts rendered and the step file has zero
  `NotImplementedError` placeholders. `autocoder generate` leaves
  URLs here; `autocoder run` only uses this state transiently before
  pytest decides verified/needs_implementation.
- **`verified`** — generation + pytest both pass (possibly after a
  number of heal attempts captured in `URLNode.heal_attempts`).
  Produced exclusively by `autocoder run`. The
  `URLNode.last_pytest_outcome` is `"pass"` and
  `URLNode.last_verified_at` carries the ISO timestamp.
- **`needs_implementation`** — all artifacts rendered, but either
  synthesis left a placeholder OR pytest still fails after
  `--max-heal-attempts`. The end-of-run summary becomes
  `run_done_with_issues` so this does not get missed. `skip_regen`
  intentionally requires `status == complete` to short-circuit, so
  `needs_implementation` URLs always regenerate on the next run.
- **`failed`** — any generation stage raised. Logged with `err_type`
  so the rest of the run continues.

`URLNode` carries three extra verification fields populated only by
`autocoder run`:

```yaml
last_verified_at: '2026-04-19T20:15:42+00:00'
last_pytest_outcome: pass    # "pass" | "fail" | ""
heal_attempts: 2             # total heal passes used for this slug
```

## Resume

`autocoder/registry/resume.py:next_actionable_nodes(...)` returns
every node that is not `complete`. The orchestrator then runs each
node through the pipeline. Because each stage is idempotent and
fingerprint-aware, a resume after a crash is safe.

## Rerun

`autocoder rerun` is `autocoder generate` over every URL already in
the registry. The interesting behaviour:

- Re-classify each URL — `kind` and `requires_auth` may have changed
  since the last run.
- Re-extract each URL — get a fresh fingerprint.
- If `fingerprint == last_fingerprint` and the node is already
  `complete`, **skip stages 4-7 entirely**. No LLM tokens spent.
- If the fingerprint changed, regenerate POM/feature/steps and
  promote to the new status.

The diff helper (`autocoder/registry/diff.py:diff_extractions`)
classifies each change so logs are explicit:

```
ChangeReport(
    added_elements=["new_button"],
    removed_elements=[],
    changed_selectors=["search"],
    headings_changed=False,
    title_changed=False,
)
```

`needs_regeneration` is true when any element was added/removed, any
selector changed, or the title changed. Heading-only changes are
ignored — they affect the LLM's prompt but not the test surface.

## Extension

`autocoder extend --tier regression --tier edge <urls>` is implemented
as a `generate(force=True)` with the existing tier list expanded:

- The POM plan cache is reused if the fingerprint matches (no need
  to re-derive the POM just to add scenarios).
- The feature plan cache key includes the tier set, so the new tier
  combination triggers a fresh feature plan.
- The validator dedupes by scenario title, so existing scenarios are
  preserved verbatim — only new ones are added.

If you omit `<urls>`, extension applies to every URL in the registry.

## Logs

Each `autocoder ...` invocation opens a fresh file under
`manifest/logs/` named `<YYYYMMDD>-<HHMMSS>-<cmd>.log` — for example
`20260419-223728-generate.log`. The file contains newline-delimited
JSON, one event per line:

```json
{"ts": 1737067321.4, "level": "info", "event": "llm_call", "model": "phi4:14b", "purpose": "pom_plan:login_page", "in_tokens": 412, "out_tokens": 124, "total_tokens": 536, "duration": "37.21s", "cached": false}
{"ts": 1737067358.1, "level": "ok",   "event": "auth_setup_written", "path": "tests/auth_setup/test_auth_setup.py"}
{"ts": 1737067512.7, "level": "warn", "event": "feature_plan_issue", "fixture": "dashboard_page", "msg": "duplicate scenario title dropped: User opens dashboard"}
```

Tail the latest:

```bash
tail -f manifest/logs/$(ls -t manifest/logs | head -1)
```

Grep for `level=warn|error` across all runs for post-mortem:

```bash
jq -r 'select(.level=="warn" or .level=="error")' manifest/logs/*.log
```

## Hand edits

Safe hand edits:

- Replace selectors in a generated `tests/generated/<run>/<slug>/<slug>_page.py`
  — preserved across reruns until the extraction fingerprint changes.
  Each generation writes to a fresh `<run>` folder, so edits to an
  older bundle are ignored by pytest/heal (the conftest filter picks
  the newest bundle per slug).
- Add manual scenarios to a generated `tests/generated/<run>/<slug>/<slug>.feature`
  — preserved across reruns *as long as* the orchestrator does not
  regenerate that feature. To prevent regeneration, mark the URL's
  status as `complete` in `registry.yaml`.
- Set `URLNode.depends_on` manually for cases the classifier does not
  pick up automatically.

Unsafe hand edits:

- Editing `manifest/extractions/<slug>.json` (will be overwritten on
  next extraction).
- Editing `manifest/plans/*.json` (will be overwritten on next plan).
