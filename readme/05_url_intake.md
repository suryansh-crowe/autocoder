# 05 · URL intake

The intake stage takes the URL list you pass to the CLI, turns it
into a set of `URLNode` rows in the registry, and produces a
deterministic processing order. No LLM tokens are spent here — every
signal comes from a real Playwright probe.

## Where the URL list comes from

`autocoder/intake/sources.py:resolve_urls(...)` is the single resolver
used by both `generate` and `extend`. It checks **four** sources in
priority order and uses the first non-empty one:

| Priority | Source | How |
|----------|--------|-----|
| 1 | CLI positional args | `autocoder generate <url1> <url2> ...` |
| 2 | `--urls-file <path>` | One URL per line. Blank lines and lines starting with `#` are ignored. |
| 3 | `AUTOCODER_URLS` env var | Comma- or newline-separated. Same comment / blank rules. |
| 4 | Settings fallback | `[LOGIN_URL, BASE_URL]` from `.env` (whichever are set). |

Behaviour:

- The chosen source is logged (`urls_source source=… count=…`) so you
  always know where the URL list came from.
- **Splitting is structure-aware.** Newlines and `,http(s)://`
  boundaries split the input. A comma inside a URL's query string
  (`?fields=name,email,role`) is **not** treated as a separator —
  the URL survives intact.
- All entries are deduped while preserving order.
- All entries are validated. `diagnose_url(url)` returns a one-line
  reason per failure (missing scheme → suggests `https://...`,
  unsupported scheme, missing host, parse error). The validator
  includes that reason in the error message and refuses to fall
  through to a lower-priority source.
- An empty result from every source fails the CLI with a
  four-option usage hint.

`autocoder rerun` does not use this resolver — it always loads URLs
straight from the registry. `autocoder extend` uses the resolver but
treats an empty result as "every URL in the registry".

## What gets classified

`autocoder/intake/classifier.py` opens each URL anonymously (no
`storage_state`) and sets the node's `kind`:

| Kind                  | Detected when                                                    |
|-----------------------|------------------------------------------------------------------|
| `LOGIN`               | The page exposes a password input and a username-ish field.      |
| `REDIRECT_TO_LOGIN`   | The URL redirects to a login-shaped URL. Marks `requires_auth`.  |
| `PUBLIC`              | Loads without redirecting and shows no login form.               |
| `POST_LOGIN_LANDING`  | Reserved for nodes the orchestrator promotes after auth setup.   |
| `AUTHENTICATED`       | Reserved for explicit user override in the registry.             |
| `UNKNOWN`             | Probe failed (timeout, network error). Captured for retry.       |

The classifier uses a small set of robust heuristics — login URL hints
in the path (`login`, `signin`, `sso`, `auth`, …) and a check for an
`<input type="password">` paired with a username-shaped field. False
positives are cheap (one extra auth attempt); false negatives are
caught at extraction time when the page redirects to login.

## Login URL discovery

If your `.env` does not set `LOGIN_URL`, the classifier returns
`detected_login_url` from the first probe that lands on a login form
(either directly or via redirect). The orchestrator stores that on
`Registry.auth.login_url`. From then on the system trusts the
registry.

## Dependency graph

`autocoder/intake/graph.py` builds an edge set:

- Every authenticated URL depends on the login URL.
- Any URL whose probe redirected to another tracked URL depends on
  that target.
- Manual `depends_on` entries you write directly in the registry are
  honoured.

Then `topological_order(...)` runs Kahn's algorithm with one tiebreak:

1. URLs of kind `LOGIN` sort first.
2. URLs of kind `POST_LOGIN_LANDING` sort second.
3. Everything else sorts last.

This means a fresh run on `[/dashboard, /login]` always processes
`/login` first, no matter the order on the command line.

## What the registry looks like after intake

```yaml
version: 1
base_url: https://app.example.com
auth:
  login_url: https://app.example.com/login
  storage_state_path: .auth/user.json
  status: pending
nodes:
  https://app.example.com/login:
    url: https://app.example.com/login
    slug: login
    kind: login
    requires_auth: false
    status: pending
  https://app.example.com/dashboard:
    url: https://app.example.com/dashboard
    slug: dashboard
    kind: redirect_to_login
    requires_auth: true
    redirects_to: https://app.example.com/login
    depends_on: [https://app.example.com/login]
    status: pending
```

The orchestrator advances `status` per stage as the URL moves through
extraction, plan, render, and persist.

## Reclassification on rerun

Reruns re-probe every URL because `kind` and `requires_auth` can change
when the app changes (a public page becomes protected, an SSO redirect
chain shifts, etc.). Existing status fields are preserved by
`RegistryStore.upsert_node` so reruns do not lose progress.
