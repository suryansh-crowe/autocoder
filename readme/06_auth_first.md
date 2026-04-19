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

| `auth_kind`          | Recognised shape                                                                         | Required env                              |
|----------------------|------------------------------------------------------------------------------------------|-------------------------------------------|
| `form`               | Inline `input[type=password]` + username input + submit.                                 | `LOGIN_USERNAME`, `LOGIN_PASSWORD`        |
| `magic_link`         | Explicit "Send magic link" / "Email me a link" button.                                   | `LOGIN_USERNAME` (+ human to click link)  |
| `otp_code`           | "Send code" / "Email me a code" button.                                                  | `LOGIN_USERNAME` (+ human to enter code)  |
| `sso_microsoft`      | "Sign in with Microsoft" / "Continue with Microsoft" button.                             | `LOGIN_USERNAME`, `LOGIN_PASSWORD`        |
| `sso_generic`        | Google/GitHub/generic-SSO button.                                                        | `LOGIN_USERNAME`, `LOGIN_PASSWORD`        |
| `username_first`     | Username/email input + "Next"/"Continue" button, no password.                            | `LOGIN_USERNAME` (+ password if 2nd step asks) |
| `email_only`         | Single `<input type=email>` + submit, no password, no provider button.                   | `LOGIN_USERNAME` (+ human)                |
| `unknown_auth`       | Login-shaped page that matched none of the above. Best-effort scaffold only.             | depends                                   |

The decision is logged as `auth_mode_detected auth_kind=<mode>
requires_external_completion=<bool>` with every selector strategy we
picked up. Non-password modes carry `notes=…` describing what the user
still has to do.

## In-process auth runner

`autocoder/extract/auth_runner.py:run_auth(spec, settings)` executes
the detected flow with a fresh Playwright context (no storage), then
saves the resulting `storage_state` to `settings.paths.storage_state`
(default `.auth/user.json`).

Dispatch per mode:

- **`form`** — fill username, fill password, click submit.
- **`sso_microsoft`** — click the provider button; follow the redirect
  (or the popup) to `login.microsoftonline.com`; fill `loginfmt`, click
  Next, fill `passwd`, click Sign in, best-effort click the "Stay
  signed in?" prompt. Selectors can be overridden per tenant via
  `AUTH_MSFT_EMAIL_SELECTOR` / `AUTH_MSFT_NEXT_SELECTOR` /
  `AUTH_MSFT_PASSWORD_SELECTOR` / `AUTH_MSFT_SUBMIT_SELECTOR` /
  `AUTH_MSFT_KMSI_SELECTOR`.
- **`sso_generic`** — click the button; if a password input appears at
  the destination, fill and submit.
- **`username_first`** — fill username, click Next, then wait up to
  30 s for a password input. If it appears and `LOGIN_PASSWORD` is
  set, fill + submit. If the next screen is an IdP redirect or a code
  challenge, return `awaiting_external_completion`.
- **`email_only` / `magic_link` / `otp_code`** — fill the email, click
  the continue/submit button, return `awaiting_external_completion`.
  Whatever cookies the server set so far are still written to
  `storage_state` so the user's manual follow-up step starts warm.

Credential gating is mode-aware (`_credentials` in `auth_runner.py`):
a password is **only** required for `form` / `sso_*`. Username-only
modes accept just `LOGIN_USERNAME`.

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
auth_session_captured             # storage saved — 
auth_post_capture_invalidated     # stale PUBLIC nodes marked for re-extract
auth_session_awaiting_external    # flow paused for manual step
auth_session_not_captured         # missing creds / unreachable / failed
auth_escalation_retry             # extraction hit login — retry under session
auth_failure_artifacts            # screenshot + HTML written on runner failure
```
