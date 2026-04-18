# 06 · Auth-first handling

If any URL in the input requires authentication, the orchestrator
finishes the auth setup before it touches any protected page. The
mechanism is "auth-first" because no extraction, plan, or render
happens for protected URLs until a `storage_state` exists.

## When auth-first kicks in

In the orchestrator (`autocoder/orchestrator.py`):

1. After classification, `_maybe_seed_auth(...)` checks whether
   `any(node.requires_auth)` is true.
2. If yes, it seeds `Registry.auth = AuthSpec(login_url=...)`.
   `login_url` comes from (a) `LOGIN_URL` in `.env`, (b) the
   classifier's `detected_login_url`, or (c) the URL list itself
   (any URL classified as `LOGIN`).
3. `_materialise_auth(...)` then opens the login URL anonymously,
   probes for a username field + password field + submit affordance,
   builds an `AuthSpec`, and renders
   `tests/auth_setup/test_auth_setup.py`.
4. When the orchestrator iterates URLs, the first URL whose `kind` is
   `LOGIN` and whose URL matches `auth.login_url` is marked
   `complete` immediately — the auth-setup test covers it.

## Form detection

`autocoder/extract/auth_probe.py` walks the page looking for:

- `input[type="password"]` (visible) → password selector.
- A username-ish input around the password field
  (`type="email"`, `name|id|autocomplete *= email|user|login|...`).
- A submit affordance (`button[type=submit]`, then any form button,
  then any button as a fallback).

Each is captured with the same selector strategy used everywhere else
(see `08_selectors_and_self_healing.md`).

## The generated auth setup

`autocoder/generate/auth_setup.py` renders a single pytest test —
`tests/auth_setup/test_auth_setup.py`. The body looks like:

```python
@pytest.mark.auth_setup
def test_auth_setup(page: Page) -> None:
    username = _need("LOGIN_USERNAME")
    password = _need("LOGIN_PASSWORD")
    page.goto(_LOGIN_URL)
    page.get_by_test_id("email").fill(username)
    page.get_by_label("Password").fill(password)
    page.get_by_role("button", name="Sign in").click()
    page.wait_for_load_state("networkidle")
    page.context.storage_state(path=str(_STORAGE_STATE))
```

Run it once after generation (or whenever credentials rotate):

```bash
pytest tests/auth_setup -m auth_setup
```

The output, `tests/.auth/user.json`, is the cached session every other
test inherits via `storage_state` (see `tests/conftest.py`).

## Secret handling rules

The system enforces a hard rule: **secrets never leave the local
process environment.** Specifically:

- The renderer never embeds credential values; it only emits
  `os.environ.get(...)` lookups.
- The classifier and inspector never read `LOGIN_USERNAME` /
  `LOGIN_PASSWORD`.
- The LLM prompts contain no env var values.
- `manifest/registry.yaml` and `manifest/runs.log` only record env
  *names*, never values.
- `.env` is gitignored. `.env.example` ships placeholders only.

`tests/support/env.py` exposes `require(name)` which raises a clear
error if a secret is missing. Generated tests use that instead of
`os.environ[...]`.

## Multiple roles (RBAC)

For role-based scenarios, define extra credentials in `.env`:

```env
RBAC_USERNAME=...
RBAC_PASSWORD=...
```

Then either render a second auth-setup test by overriding
`AuthSpec.username_env` / `password_env` on a forked registry, or
parametrise the existing setup. The orchestrator does not generate
multi-role auth automatically yet; this is the documented extension
point.
