# 15 · Logging and runtime traceability

The system is fully traceable end-to-end. Every stage emits structured
events to two sinks at the same time:

- **Console** (stderr, coloured) — for the human running the script.
- **Per-invocation log file** under `manifest/logs/`, named
  `<YYYYMMDD>-<HHMMSS>-<cmd>.log` (e.g.
  `20260419-223728-generate.log`). Newline-delimited JSON, one event
  per line. Each `autocoder ...` invocation gets its own file — no
  more single growing log. The CLI prints the active path on the
  first `cli_invoke` event so you can `tail -f` it directly.

Both sinks share the same schema, so anything you see live can also
be reviewed later from disk.

## Log levels

| Level   | When to use                                                   | Console colour |
|---------|---------------------------------------------------------------|----------------|
| `debug` | Per-element selector picks, raw HTTP details, cache writes    | dim            |
| `info`  | Stage transitions, decisions ("auth seeded", "diff_report")   | cyan           |
| `ok`    | A stage finished successfully                                 | green          |
| `warn`  | Something is off but the run continues                        | yellow         |
| `error` | The current stage / URL failed; run continues with the others | red            |

Set the floor with `LOG_LEVEL` in `.env` (or as an env var). Default
is `info`. Use `debug` when investigating selector drift, plan-cache
behaviour, or LLM payload shapes.

```env
# .env
LOG_LEVEL=info       # debug | info | warn | error
```

## Log line shape

Console:

```
 info classify_done nodes=3 login_detected=True kind_login=1 kind_redirect_to_login=2
   ok pom_written slug=dashboard path=tests/pages/dashboard_page.py action=created methods=8
 warn feature_plan_issue fixture=dashboard_page msg=duplicate scenario title dropped: User opens dashboard
```

`manifest/logs/<ts>-<cmd>.log` (one JSON object per line):

```json
{"ts": 1737067321.4, "level": "info", "event": "llm_call",
 "model": "phi4:14b", "purpose": "pom_plan:dashboard_page",
 "in_tokens": 412, "out_tokens": 124, "total_tokens": 536,
 "duration": "37.21s", "cached": false}
```

## Token accounting (LLM)

Every LLM invocation emits one `llm_call` event with a uniform shape:

| Field          | Meaning                                              |
|----------------|------------------------------------------------------|
| `model`        | The model that served the request (`phi4:14b`, or `(cache)` when reused). |
| `purpose`      | What the call was for. Examples: `pom_plan:dashboard_page`, `feature_plan:dashboard_page`. |
| `in_tokens`    | Prompt tokens consumed.                              |
| `out_tokens`   | Completion tokens produced.                          |
| `total_tokens` | Sum of the two.                                      |
| `duration`     | Wall time as `"NN.NNs"`.                             |
| `cached`       | `true` when the on-disk plan cache satisfied the call (zero spend). |

Quick token totals from the latest run:

```bash
LATEST=manifest/logs/$(ls -t manifest/logs | head -1)

# Total tokens spent in the latest run
jq 'select(.event=="llm_call" and .cached==false) | .total_tokens' \
   "$LATEST" | paste -sd+ - | bc

# Per-purpose breakdown
jq -r 'select(.event=="llm_call") | "\(.purpose) \(.total_tokens) cached=\(.cached)"' \
   "$LATEST"
```

Across all runs (cumulative):

```bash
jq -s 'map(select(.event=="llm_call" and .cached==false) | .total_tokens) | add' \
   manifest/logs/*.log
```

## Decision logging

The system explicitly logs *why* it did what it did. Look for these
events when you want to know "why?":

| Event                         | What you learn                              |
|-------------------------------|---------------------------------------------|
| `classify`                    | Why a URL was classified as login / public / redirect-to-login. The `reason` field is human-readable; `wait_strategy` shows which `goto_resilient` tier succeeded. |
| `classify_timeout` / `classify_timeout_login_inferred` / `classify_timeout_escalated_to_auth` | Nav timed out; whether a URL-path hint or a configured `LOGIN_URL` let us escalate. |
| `nav_timeout_artifacts`       | Path base for screenshot + HTML written when `goto_resilient` timed out at the `commit` tier. |
| `stage:homepage_probe` / `homepage_probe_auth_detected` / `homepage_probe_clear` | Whether the base URL itself is gated. |
| `stage:auth_first`            | Whether any login signal exists and whether `needs_auth` is true. |
| `auth_seeded`                 | Where the login URL came from (`env:LOGIN_URL` / `classifier_detection` / `node:<slug>:login` / `node:<slug>:path_hint`). |
| `auth_probe_navigated` / `auth_probe_failed` | Whether we reached the login page; failure payload includes redirect chain, popup URLs, console errors, failed requests. |
| `auth_mode_detected`          | Detected `auth_kind`, all captured selectors, `requires_external_completion`, whether credentials are present. Subsumes the older `auth_form_detected`. |
| `auth_probe_magic_link_detected` / `auth_probe_otp_detected` / `auth_probe_sso_detected` / `auth_probe_username_first_detected` / `auth_probe_email_only_detected` | Subtype signals for the relevant detection branch. |
| `classify_auth_gated_shell`   | Anonymous page carried an SSO / sign-in affordance; `requires_auth=True` was set without changing `kind`. |
| `auth_setup_written`          | Rendered `tests/auth_setup/test_auth_setup.py` action (created / updated). |
| `auth_runner_start`           | Live login attempt begins; logs `auth_kind`, `app_password_required`, credential presence, `interactive_timeout_ms`, `headless`, `mode_supports_interactive`. |
| `auth_sso_headless`           | Warning: SSO mode + `HEADLESS=true` — MFA cannot be completed. |
| `auth_sso_password_input_absent` | Entra page did not render a password input within 15s. Usually passwordless / MFA-first tenant. |
| `auth_sso_password_requested_but_absent` | Entra asked for a password but `LOGIN_PASSWORD` is not configured; waiting for interactive entry. |
| `auth_awaiting_success`       | Runner has finished what it can automate; now watching for the post-auth URL signal within `timeout_ms`. Complete MFA at this point if prompted. |
| `url_skipped_awaiting_auth`   | Protected URL skipped because `requires_auth=True` but no storage state has been captured yet. |
| `auth_session_captured`       | Login succeeded; `.auth/user.json` written. |
| `auth_settle_start` / `auth_settle_done` / `auth_settle_skipped` | Proactive nav to `base_url` after capture so extraction starts from a hydrated SPA, not the OAuth return URL. `done` carries `final_url` + `on_login_shell`. |
| `auth_settle_silent_reauth`   | Base URL still rendered the SSO shell; runner clicked the provider button once so MSAL rehydrates (no MFA — tokens cached). |
| `auth_settle_nav_failed`      | `goto_resilient(base_url)` raised; per-URL extraction will retry with storage_state anyway. |
| `auth_reset_done`             | `autocoder auth-reset` completed — lists removed files and whether the spec was wiped. |
| `auth_post_capture_invalidated` | Count of non-LOGIN nodes whose status was reset so re-extraction happens under the new session. |
| `auth_session_awaiting_external` | Runner reached a step it cannot automate (magic link, OTP, MFA). Any cookies set so far have been persisted. |
| `auth_session_not_captured`   | Runner bailed (`missing_credentials`, `missing_password_for_password_mode`, `login_page_unreachable`, `success_indicator_not_seen`, ...). |
| `auth_failure_artifacts`      | Path base for screenshot + HTML dumped on runner failure. |
| `auth_escalation_retry` / `auth_escalation_succeeded` / `auth_escalation_failed` / `auth_escalation_materialise` / `auth_escalation_no_login_url` / `auth_escalation_no_storage` | Extraction hit a login page; whether we seeded auth and retried under session. |
| `extract_redirected_to_login` | Anonymous extraction landed on a login-shaped URL. |
| `extraction_storage_decision` | Whether storage was loaded for this URL, and why (`requires_auth` / `kind=authenticated` / `auth_ready_session_reuse` / `anonymous`). |
| `selector_picked`             | Which strategy won for a given element (debug level). |
| `selector_fragile`            | Primary selector fell back to CSS or XPath — surfaced for review (debug level). |
| `diff_report`                 | Per-URL change summary vs. previous run.    |
| `rerun_unchanged`             | Why an already-complete URL was skipped.    |
| `pom_plan_cache_hit` / `_miss` | Why a stage spent or saved LLM tokens.     |
| `ollama_json_retry` / `ollama_json_recovered` / `ollama_json_parse_failed` | JSON recovery ladder progress; the `recovered` case means the strict-prompt retry parsed successfully. |
| `feature_plan_fallback`       | `OllamaError` on the feature plan was caught; a minimal `FeaturePlan` was substituted so the POM + steps still render. |
| `pom_written` / `feature_written` / `steps_written` | `action=created` vs. `action=updated`, plus counts. `steps_written` additionally carries `status` (`complete` / `needs_implementation`) and `placeholders` count. |
| `steps_syntax_error`          | Rendered module failed `ast.parse` before writing; `_strip_step_bodies_for_heal` rewrote every body to `NotImplementedError`. |
| `steps_write_aborted`         | Stripped fallback also failed to parse; node marked `failed` and nothing written. |
| `steps_incomplete`            | Quality gate fired: the rendered step file still has ≥ 1 placeholder body. |
| `steps_autoheal` / `steps_autoheal_done` / `steps_autoheal_failed` | Orchestrator's inline LLM heal pass that runs right after rendering. `done` carries `stubs`, `applied`, `remaining_placeholders`. |
| `run_done` / `run_done_with_issues` | Terminal summary. `run_done_with_issues` is used whenever any URL ended up `needs_implementation` or `failed`. |
| `urls_source`                 | Which URL source the CLI used (`cli` / `file:…` / `env` / `settings`). |
| `url_skipped`                 | Why an in-order URL was skipped (e.g. `login_url_covered_by_auth_setup`). |
| `url_failed`                  | A URL's processing raised; logged with `err_type` so the run keeps going. |
| `heal_context_loaded`         | Per-slug POM methods + element catalog the heal LLM will see. |
| `heal_forbidden_ids`          | Element ids (per stub) that prior When/And steps in the same scenario acted on — plus name-token siblings. The heal LLM is forbidden from asserting against these. |
| `heal_fail_cache_busted`      | The failure-heal cached body already matches the CURRENT on-disk body and the test is still failing. Cache ignored; LLM re-called with fresh context. |
| `heal_dry_run` / `heal_applied` | What the LLM proposed and whether it was written. |
| `heal_invalid_body`           | Why a suggestion was rejected — the chained-non-assertion rule (`.to_be_visible().click()`), the stub-heal URL-assert ban, the trivial-URL rule, or the forbidden-id rule. Rejected bodies fall back to `pass  # no safe binding` (stub heal AND failure heal). |
| `heal_apply_failed`           | Generated source did not re-parse; original kept untouched. |
| `heal_pytest_run` / `heal_failures_collected` | `--from-pytest` invocation + parsed failure count. |
| `report_pytest_run` / `report_pytest_failed` / `report_pytest_skipped_missing` | `autocoder report --run` per-slug pytest status. |
| `report_html_written`         | HTML dashboard path written by `autocoder report --html`. |

## Stage markers

For quick scanning of a run, every stage opens with a
`stage:<name>` event:

```
stage:run_start          urls=3 tiers=smoke,happy,validation force=False ...
stage:intake             urls=3
stage:auth_first         needs_auth=True
stage:url_begin          position=1/3 slug=login url=https://app.example.com/login kind=login ...
stage:pom_plan           slug=dashboard fixture=dashboard_page elements=24
stage:feature_plan       slug=dashboard tiers=smoke,happy,validation pom_methods=8
```

Filter the latest run log by stage:

```bash
jq -r 'select(.event | startswith("stage:")) | "\(.event) \(.slug // "")"' \
   manifest/logs/$(ls -t manifest/logs | head -1)
```

## What is *never* logged

Hard rule: **credential values never leave the process environment.**
Specifically, the system never logs:

- `LOGIN_USERNAME`, `LOGIN_PASSWORD`, `LOGIN_OTP_SECRET`, `RBAC_*`
  values — only their presence (`username_env_present=True/False`) or
  the env var **name** (`username_env="LOGIN_USERNAME"`).
- Raw `.env` contents — only the parsed setting values that are not
  secrets (`base_url`, `OLLAMA_MODEL`, etc.).
- Authorization headers, API keys, bearer tokens, or session cookies.
- URL query strings — the `safe_url(url)` helper strips
  `?...` and `#...` from every URL field before logging, since query
  strings are the most common place to find one-time codes or
  session identifiers.
- LLM prompt / response bodies — only character counts (`sys_chars`,
  `user_chars`) and token counts. No prompt text, no response text.

If you ever spot a credential value in any `manifest/logs/*.log`,
treat it as a bug. Open the offending line, identify the call site,
and fix it (usually by replacing a value with `secret_present(name)`
or a name string).

## How to use the log day-to-day

```bash
# Live-tail during a run (in a second terminal)
tail -f manifest/logs/$(ls -t manifest/logs | head -1) \
  | jq -r '"\(.level | ascii_upcase) \(.event) \(. | tostring)"'

# Show only warnings + errors across every run
jq -r 'select(.level=="warn" or .level=="error") | "\(input_filename) \(.event) \(. | tostring)"' \
   manifest/logs/*.log | tail -50

# Count cache hits vs LLM calls by purpose, across every run
jq -r 'select(.event=="llm_call") | "\(.purpose) cached=\(.cached)"' \
   manifest/logs/*.log | sort | uniq -c | sort -rn

# Audit secret handling across every run — should return zero matches
jq -r '. | tostring' manifest/logs/*.log \
  | grep -E '(password|secret|token)=[^ ]' \
  | grep -v 'present=' | grep -v 'env='
```

## Centralisation

Every module imports the same `from autocoder import logger` and
calls `logger.info(...)` / `logger.ok(...)` / `logger.warn(...)` /
`logger.error(...)` / `logger.debug(...)`. There is exactly one
formatter and exactly one sink set per process. Adding a new event
is just `logger.info("my_event", key="value", ...)` — it shows up in
both sinks automatically.
