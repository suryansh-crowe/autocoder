# 06 · Auth-first handling

If any URL in the input requires authentication — or if there is any
other signal that auth is relevant (an explicit `LOGIN_URL`, a
login-shaped path, a homepage that redirects to login) — the
orchestrator does three things before touching the rest of the
pipeline:

1. Detects the shape of the login page.
2. Writes a runnable `tests/auth_setup/test_auth_setup.py` tailored to
   that shape.
3. **Actually performs the login in-process** and captures
   `storage_state` to `.auth/user.json`. The rest of the run reads
   that file; no manual `pytest tests/auth_setup` step is required.

"Auth-first" means no extraction, plan, or render for a protected URL
happens until that sequence has at least tried.

## When auth-first kicks in

In `autocoder/orchestrator.py`:

1. After classification, `_probe_homepage(...)` checks whether the
   `base_url` itself is gated (it is run only when `base_url` is not
   already one of the input URLs). A redirect to a login-shaped URL
   adds a strong signal.
2. `_maybe_seed_auth(...)` then considers **every** signal at once:
   - `LOGIN_URL` in `.env`
   - the classifier's `detected_login_url`
   - any node with `kind=LOGIN` or `kind=REDIRECT_TO_LOGIN`
   - any classifier-UNKNOWN node whose URL path matches
     `login|signin|sso|auth|oauth|openid`
   - an explicit `requires_auth=True` on any node
3. If any signal fires, `registry.auth = AuthSpec(login_url=..., auth_kind=...)` is seeded and the auth-first stage runs.

The top-level event to look for is `stage:auth_first has_login_signal=True`.

## Auth mode inference

`autocoder/extract/auth_probe.py:build_auth_spec(page, ...)` opens the
login URL anonymously and classifies it into one of eight modes. The
first rule to match wins:

| `auth_kind`          | Recognised shape                                                                         | Required env                                             |
|----------------------|------------------------------------------------------------------------------------------|----------------------------------------------------------|
| `form`               | Inline `input[type=password]` + username input + submit.                                 | `LOGIN_USERNAME`, `LOGIN_PASSWORD`                       |
| `magic_link`         | Explicit "Send magic link" / "Email me a link" button.                                   | `LOGIN_USERNAME` (+ human to click link)                 |
| `otp_code`           | "Send code" / "Email me a code" button.                                                  | `LOGIN_USERNAME` (+ human to enter code)                 |
| `sso_microsoft`      | "Sign in with Microsoft" / "Continue with Microsoft" button.                             | `LOGIN_USERNAME` (password optional; MFA handled interactively) |
| `sso_generic`        | Google/GitHub/generic-SSO button.                                                        | `LOGIN_USERNAME` (password optional; MFA handled interactively) |
| `username_first`     | Username/email input + "Next"/"Continue" button, no password.                            | `LOGIN_USERNAME` (+ password if 2nd step asks)           |
| `email_only`         | Single `<input type=email>` + submit, no password, no provider button.                   | `LOGIN_USERNAME` (+ human)                               |
| `unknown_auth`       | Login-shaped page that matched none of the above. Best-effort scaffold only.             | depends                                                  |

Only `form` actually requires a password to start the flow. SSO
modes accept username-only: the runner will fill the password on the
IdP page if it appears and a password is configured, otherwise it
hands the window to the user and waits for post-auth navigation.
Non-password modes carry `notes=…` describing what the user still
has to do.

The decision is logged as `auth_mode_detected auth_kind=<mode>
requires_external_completion=<bool>` with every selector strategy we
picked up.

## In-process auth runner

`autocoder/extract/auth_runner.py:run_auth(spec, settings)` executes
the detected flow with a fresh Playwright context (no storage), then
saves the resulting `storage_state` to `settings.paths.storage_state`
(default `.auth/user.json`).

Dispatch per mode:

- **`form`** — fill username, fill password, click submit.
- **`sso_microsoft`** — poll the app's consent checkbox until the
  "Sign in with Microsoft" button is enabled (up to 15 s — reactive
  SPAs often mount the checkbox *after* the first frame, so a one-
  shot tick at page-load time misses it). Then click the button.
  If the click still fails (disabled custom control, modal overlay,
  etc.) the runner logs a clear hint, tries a JS-level
  `el.click()` fallback, and **falls through to `_wait_success`**
  rather than aborting — you can tick the consent and click Sign
  in yourself in the visible browser, and the session still gets
  captured. After the redirect, fill `loginfmt`, click Next, then
  *best-effort* fill `passwd` if (a) the password input appears
  within 15 s and (b) `LOGIN_PASSWORD` is configured. If either
  condition fails, the runner stops typing and hands the window to
  the user — MFA prompts, passkey challenges, number-match screens,
  and conditional-access prompts all land here. Selectors can be
  overridden per tenant via `AUTH_MSFT_EMAIL_SELECTOR` /
  `AUTH_MSFT_NEXT_SELECTOR` / `AUTH_MSFT_PASSWORD_SELECTOR` /
  `AUTH_MSFT_SUBMIT_SELECTOR` / `AUTH_MSFT_KMSI_SELECTOR`.
- **`sso_generic`** — click the button; if a password input appears at
  the destination **and** `LOGIN_PASSWORD` is set, fill and submit.
  Otherwise wait for interactive completion.
- **`username_first`** — fill username, click Next, then wait up to
  30 s for a password input. If it appears and `LOGIN_PASSWORD` is
  set, fill + submit. If the next screen is an IdP redirect or a code
  challenge, return `awaiting_external_completion`.
- **`email_only` / `magic_link` / `otp_code`** — fill the email, click
  the continue/submit button, return `awaiting_external_completion`.
  Whatever cookies the server set so far are still written to
  `storage_state` so the user's manual follow-up step starts warm.

Credential gating is mode-aware (`_credentials` in `auth_runner.py`):
a password is **only** required for `form`. SSO and every other mode
accept username-only — the runner will use a password if one is
configured, otherwise it waits for the user to finish the flow.

## Interactive completion budget

`_interactive_timeout_ms(settings)` decides how long `_wait_success`
will watch the browser for the post-auth signal:

| Condition                                     | Default budget |
|-----------------------------------------------|----------------|
| Any run (headed or headless)                   | **45 s**       |
| Override                                       | `AUTH_INTERACTIVE_TIMEOUT_MS` env var (ms) |

A typical Authenticator push / number-match flow completes in well
under 45 s. When MFA takes longer in practice (ticket-based
approval, passkey prompts, slow tenants), bump the env var —
`AUTH_INTERACTIVE_TIMEOUT_MS=120000` for two minutes, etc. Failing
fast keeps the feedback loop tight: a 5-minute idle usually signals
a configuration problem (wrong `LOGIN_URL`, SPA redirect bug) that
deserves investigation, not more waiting.

When `HEADLESS=true` is combined with an SSO mode, the runner emits
`auth_sso_headless` as a warning: enterprise tenants almost always
require MFA, and a headless run has no way to satisfy it. The run
still proceeds in case the tenant relies on browser SSO cookies that
happen to be present, but expect `success_indicator_not_seen` if it
doesn't pan out.

## _wait_success signal set

`_wait_success` returns a `Page | None`. Scanning every page in the
context on each tick (so popup-based flows are captured too), it
accepts *any* of these signals as success:

1. **URL-based**. The current URL contains
   `spec.success_indicator_url_contains` (or `base_url`) and is not
   on `login.microsoftonline.com`, OR the URL has left both the
   provider domain and the app's `/login` path entirely.
2. **MSAL storage present** (`_has_msal_session(page)`). Evaluates
   `sessionStorage` + `localStorage` for any key starting with
   `msal.` or containing `login.windows.net`,
   `login.microsoftonline.com`, `account`, `idtoken`, `accesstoken`,
   or `homeaccountid`. MSAL writes the account row as part of the
   OAuth callback — its presence proves authentication completed at
   the protocol level, even when the SPA is still rendering a 404
   fallback because the app's configured `redirect_uri` is a route
   it doesn't know about.
3. **Proactive redirect nudge**. When the URL is stuck on `/login`
   for more than 8 s *and* MSAL storage is populated, the runner
   does a single `page.goto(base_url)`. The SPA mounts on a real
   route, MSAL hydrates from stored tokens, authenticated DOM
   renders, signal 1 fires.

Storage state is written to `.auth/user.json` only after one of the
three signals matches. A session that stalls on MFA never
masquerades as a captured session.

Events to watch:

```
auth_sso_button_disabled hint=polling and ticking visible consent checkboxes …
auth_sso_button_unblocked ticked=1 attempts=1 via=input:not(:checked)
auth_sso_button_still_disabled                    # poll gave up, handing off interactively
auth_sso_button_clicked auto=True
auth_sso_button_click_failed                      # click threw; falling through to _wait_success
auth_awaiting_success timeout_ms=45000
auth_success_signal via=url | msal_storage | marker_text
auth_redirect_nudge from_url=…/login to_url=…     # proactive nav kicked in
auth_success_page_found url=…/home via=main_page | popup
auth_session_captured storage_state=.auth/user.json
```

Outcomes:

- `ok` → log `auth_session_captured` + `auth_post_capture_invalidated`
  (see below). Subsequent URLs in the same run use the session.
- `awaiting_external_completion` → log `auth_session_awaiting_external`
  with a precise hint (magic link in inbox, OTP challenge, MFA…) and
  set `registry.auth.status = NEEDS_IMPLEMENTATION`. The rendered
  `test_auth_setup.py` is ready to finish the flow headful.
- `missing_credentials` / `missing_password_for_password_mode` → log
  `auth_session_not_captured` with the precise reason.
- `success_indicator_not_seen` → log artefacts (screenshot + HTML
  under `manifest/logs/auth_failure_<ts>_<kind>.*`) and bail with a
  clear reason.

## Stale-mark after auth capture

Once `run_auth` succeeds mid-run, every non-`LOGIN` node whose status
was already `complete` / `needs_implementation` / `extracted` is reset
to `pending` and its `last_fingerprint` is cleared. The orchestrator
iterates the URL list next, now with storage loaded, and re-extracts
each URL against the *authenticated* DOM. This fixes the silent
"anonymous shell masquerading as the real page" failure mode.

The event to look for: `auth_post_capture_invalidated count=<N>`.

## Settle step (proactive nav after auth capture)

Many enterprise MSAL apps register their OAuth ``redirect_uri`` as
``/login`` (or another path that's a 404 in the SPA router). When
the runner's success check fires on "MSAL tokens present in
sessionStorage", the tab is still on that 404 shell. If per-URL
extraction then starts from there, the SPA's first navigation to a
protected route can bounce straight back to `/login` — MSAL hasn't
yet had a chance to mount on a real page — and the extractor
captures the 404 DOM as if it were the real application.

`_settle_after_auth` in `orchestrator.py` runs immediately after
`auth_session_captured`:

1. If `base_url` is empty, or the post-auth URL is already inside
   `base_url` and off `/login`, no-op.
2. Otherwise `goto_resilient(base_url)` on the shared page.
3. Wait up to 15 s for any interactive element to attach so MSAL
   has time to rehydrate from stored tokens.
4. If the auth-gated shell is still showing (SSO button visible),
   run `_silent_reauth` — cheap because the tokens are already
   cached, so the MSAL handshake completes without MFA.
5. Log `auth_settle_done final_url=<url> on_login_shell=<bool>`.

After that step, every per-URL `goto_resilient` starts from a
hydrated authenticated SPA, not the raw OAuth return landing. The
per-URL extraction path still has `_silent_reauth` as a backstop for
edge cases, but `_settle_after_auth` removes the most common cause
of "extracted the 404 consent shell" bugs.

Events in order to grep for on a clean run:

```
auth_session_captured                    # tokens saved to .auth/user.json
auth_settle_start    from_url=…/login    # proactive nav kicks in
auth_settle_silent_reauth                # only when base_url also gated
auth_settle_done     final_url=…/home    # ready for extraction
```

## Storage state reuse

`tests/conftest.py` loads `.auth/user.json` into every generated test
via the Playwright `browser_context_args` fixture:

```python
@pytest.fixture
def browser_context_args(browser_context_args, base_url):
    storage_state = _storage_state_path()
    args = dict(browser_context_args)
    if base_url:
        args["base_url"] = base_url
    if storage_state.exists() and storage_state.stat().st_size > 0:
        args["storage_state"] = str(storage_state)
    return args
```

Inside `autocoder generate`, `_process_url` applies the same rule for
the *extraction* side: when `registry.auth.status == STEPS_READY` and
the storage file is on disk, every non-LOGIN node uses it (including
those classified `PUBLIC`, because the anonymous classification does
not prove the authenticated view is identical).

## Escalation when extraction reveals auth after the fact

If a URL slips past classification as `PUBLIC` but extraction actually
redirects to a login-shaped URL (detected by
`extract_redirected_to_login` in `orchestrator.py:_extract_detailed`),
the orchestrator calls `_maybe_escalate_to_auth`, which:

1. Promotes the node to `requires_auth=True`.
2. If auth has not been seeded yet, seeds it from the discovered
   redirect target.
3. If `registry.auth.status != STEPS_READY`, runs `_materialise_auth`.
4. Retries the extraction once with storage attached.

Events: `auth_escalation_retry`, `auth_escalation_succeeded`, and
`auth_escalation_failed` with the same diagnostics payload
(`final_url`, `redirects`, `err`).

## What auth-first does *not* generate

The login URL itself never gets a POM, feature file, or step module.
In the per-URL loop (`orchestrator.py:195`), any URL whose `kind ==
LOGIN` and whose `url == registry.auth.login_url` is short-circuited
to `status=COMPLETE` with reason `login_url_covered_by_auth_setup`.
This is intentional: the auth-setup test already exercises the full
login flow, so a duplicate `tests/generated/<run>/login/login.feature` would be
redundant and would race for the same storage_state file.

Consequence: if you want Gherkin scenarios that assert *login-page*
behavior (for example: "Given I am on the login page, Then the
Microsoft SSO button is visible"), you have to author those by hand
or point `autocoder generate` at a *different* login-adjacent URL
(e.g. `/login/help`). The tool deliberately does not synthesize
login-page scenarios today.

## The generated auth-setup test

`autocoder/generate/auth_setup.py` ships four templates, selected by
`auth_kind`:

| `auth_kind`               | Template                           | Body summary                                                          |
|---------------------------|------------------------------------|-----------------------------------------------------------------------|
| `form` / `unknown_auth`   | `_TEMPLATE_FORM`                   | Fill username + password, click submit, wait, save storage.           |
| `sso_microsoft` / `sso_generic` | `_TEMPLATE_SSO_MS`           | Click SSO button, drive Entra page, handle popup, save storage.       |
| `username_first`          | `_TEMPLATE_USERNAME_FIRST`         | Fill username, click Next, wait for password, fill if env set, save.  |
| `email_only` / `magic_link` / `otp_code` | `_TEMPLATE_EMAIL_ONLY` | Fill email, click send. `page.wait_for_url(lambda u: "/login" not in u, timeout=300_000)` gives the user 5 minutes to complete the external step. |

All templates read credentials via `os.environ.get(...)` — secrets are
never embedded — and write `.auth/user.json` at the end. Run manually
whenever you want a fresh session:

```bash
# For modes the runner can fully automate, this is redundant — the
# orchestrator already ran it. For awaiting_external modes, this is
# where the user completes the flow in a visible browser.
HEADLESS=false pytest tests/auth_setup -m auth_setup
```

## Secret handling rules

The hard rule is unchanged: **secrets never leave the local process
environment.**

- Renderer never embeds credential values; only `os.environ.get(...)`
  lookups.
- Classifier, inspector, and LLM prompts never read
  `LOGIN_USERNAME` / `LOGIN_PASSWORD`.
- `manifest/registry.yaml` and `manifest/logs/*.log` record only env
  *names* and presence booleans (`username_env_present=true`).
- `.env` is gitignored. `.env.example` ships placeholders only.

## Multiple roles (RBAC)

Extra credentials in `.env`:

```env
RBAC_USERNAME=...
RBAC_PASSWORD=...
```

Then fork the registry with an alternate `AuthSpec.username_env` /
`password_env` and capture a second storage state to a different path.
The orchestrator does not currently render multi-role auth
automatically; this remains the documented extension point.

## Key events to grep for

```
stage:homepage_probe              # 1b kicked in
homepage_probe_auth_detected      # base URL is gated
homepage_probe_clear              # base URL is public
auth_seeded                       # login URL chosen, and from where
auth_probe_navigated              # login page reached, with nav diag
auth_mode_detected                # mode + every captured selector
auth_probe_sso_detected           # subset: SSO button found
auth_probe_magic_link_detected    # subset: magic-link phrasing
auth_probe_otp_detected           # subset: OTP phrasing
auth_probe_username_first_detected
auth_probe_email_only_detected
auth_setup_written                # template rendered
auth_runner_start                 # live login attempt begins
auth_sso_headless                 # SSO detected + HEADLESS=true warning
auth_sso_password_input_absent    # Entra did not show a password input — passwordless/MFA
auth_sso_password_requested_but_absent  # Entra asked for a password but none is configured
auth_awaiting_success             # runner is now waiting for MFA / final navigation
auth_session_captured             # storage saved — success
auth_settle_start                 # proactive nav off the OAuth return URL
auth_settle_silent_reauth         # base_url still shows the shell; MSAL re-click
auth_settle_nav_failed            # the base_url nav raised (extraction will retry)
auth_settle_done                  # browser on a real authenticated route
auth_settle_skipped               # post-auth URL was already good
auth_post_capture_invalidated     # stale PUBLIC nodes marked for re-extract
auth_session_awaiting_external    # flow paused for manual step
auth_session_not_captured         # missing creds / unreachable / failed
auth_escalation_retry             # extraction hit login — retry under session
auth_failure_artifacts            # screenshot + HTML written on runner failure
classify_auth_gated_shell         # anonymous page had a visible SSO affordance
url_skipped_awaiting_auth         # protected URL skipped — no session yet
```
