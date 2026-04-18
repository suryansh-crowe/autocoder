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
| `classify`                    | Why a URL was classified as login / public / redirect-to-login. The `reason` field is human-readable. |
| `auth_seeded`                 | Where the login URL came from (`env:LOGIN_URL` / `classifier_detection` / `input_url_list`). |
| `auth_form_detected`          | Which strategy resolved each of username / password / submit. |
| `extraction_storage_decision` | Whether the storage-state file was loaded for this URL, and why. |
| `selector_picked`             | Which strategy won for a given element (debug level). |
| `selector_fragile`            | Primary selector fell back to CSS or XPath — surfaced for review (debug level). |
| `diff_report`                 | Per-URL change summary vs. previous run.    |
| `rerun_unchanged`             | Why an already-complete URL was skipped.    |
| `pom_plan_cache_hit` / `_miss` | Why a stage spent or saved LLM tokens.     |
| `pom_written` / `feature_written` / `steps_written` | `action=created` vs. `action=updated`, plus counts. |
| `urls_source`                 | Which URL source the CLI used (`cli` / `file:…` / `env` / `settings`). |
| `url_skipped`                 | Why an in-order URL was skipped (e.g. `login_url_covered_by_auth_setup`). |
| `url_failed`                  | A URL's processing raised; logged with `err_type` so the run keeps going. |
| `heal_context_loaded`         | Per-slug POM methods + element catalog the heal LLM will see. |
| `heal_dry_run` / `heal_applied` | What the LLM proposed and whether it was written. |
| `heal_invalid_body`           | Why a suggestion was rejected (validator reason). |
| `heal_apply_failed`           | Generated source did not re-parse; original kept untouched. |
| `heal_pytest_run` / `heal_failures_collected` | `--from-pytest` invocation + parsed failure count. |

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
