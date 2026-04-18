"""URL source resolution.

The orchestrator can take URLs from four places, in priority order:

1. **CLI arguments** — explicit and wins everything else.
2. **`--urls-file <path>`** — a plain-text file, one URL per line.
   Blank lines are skipped. Lines starting with ``#`` are comments.
3. **`AUTOCODER_URLS` env var** — comma- or newline-separated list.
   Same comment / blank-line rules as the file.
4. **Settings fallback** — when nothing else matches, fall back to
   ``[LOGIN_URL, BASE_URL]`` (whichever are set in the env). This is
   the path most users hit when they only configure the standard
   ``.env`` keys without touching ``AUTOCODER_URLS``.

This module is the single place that knows about all four. The CLI
calls :func:`resolve_urls` and gets back a deduped, validated list
plus the source it came from (used for logging).

URL parsing is done with `urllib.parse` semantics throughout — never
naive string splits. Splitting respects URL boundaries so URLs whose
own query strings contain commas (`?fields=a,b,c`) survive intact.
Validation reports each malformed URL with a specific reason
(missing scheme, unsupported scheme, missing host, parse error) and
the source it came from so the user knows where to fix it.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


ENV_VAR = "AUTOCODER_URLS"

_VALID_SCHEMES = ("http", "https")

# Splitter for URL list blobs.
#
# We split on:
#   * any run of newline characters, OR
#   * a comma followed by optional whitespace and a URL scheme
#     (`http://` / `https://`).
#
# That second arm is the key fix: a comma inside a URL's query string
# (e.g. `?fields=name,email,role`) is NOT followed by `http(s)://`, so
# the regex leaves it alone. A comma between two real URLs IS followed
# by `http(s)://`, so it splits cleanly.
_LIST_SPLIT_RE = re.compile(r"[\r\n]+|,\s*(?=https?://)", re.IGNORECASE)


class URLSourceError(ValueError):
    """Raised when a URL source produces invalid input."""


@dataclass(frozen=True)
class ResolvedURLs:
    urls: list[str]
    source: str  # "cli", "file:<path>", "env", "settings", or "none"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_url_list(text: str | None) -> list[str]:
    """Split a comma- or newline-separated URL blob into clean entries.

    The split is **structure-aware**: only newlines and ``,http(s)://``
    boundaries break the input. Commas embedded inside a URL's query
    or fragment are preserved.

    Strips whitespace, drops blanks, drops ``#`` comment lines.
    Preserves order and removes duplicates.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for raw in _LIST_SPLIT_RE.split(text):
        line = raw.strip().rstrip(",").strip()
        if not line or line.startswith("#"):
            continue
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
    return out


def read_urls_file(path: Path) -> list[str]:
    """Read URLs from a file. Raises :class:`URLSourceError` if missing."""
    if not path.exists():
        raise URLSourceError(f"--urls-file not found: {path}")
    if not path.is_file():
        raise URLSourceError(f"--urls-file is not a file: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise URLSourceError(f"cannot read --urls-file {path}: {exc!s}") from exc
    return parse_url_list(text)


def read_urls_env(env: dict[str, str] | None = None) -> list[str]:
    """Read URLs from the :data:`ENV_VAR` env var (defaults to ``os.environ``)."""
    src = env if env is not None else os.environ
    return parse_url_list(src.get(ENV_VAR))


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def diagnose_url(url: str) -> str | None:
    """Return ``None`` if ``url`` is valid, else a one-line reason.

    Reasons are user-actionable and never include credential values.
    """
    if not isinstance(url, str) or not url.strip():
        return "empty input"
    candidate = url.strip()
    try:
        parsed = urlparse(candidate)
    except (ValueError, TypeError) as exc:
        return f"unparseable ({exc!s})"

    scheme = (parsed.scheme or "").lower()
    if not scheme:
        # Common mistake: pasted host with no scheme. Suggest the fix.
        return f"missing http/https scheme — try 'https://{candidate}'"
    if scheme not in _VALID_SCHEMES:
        return f"unsupported scheme {scheme!r} (only http/https are accepted)"
    if not (parsed.hostname or parsed.netloc):
        return "missing host"
    return None


def validate_urls(urls: list[str], source: str) -> list[str]:
    """Reject malformed URLs. Returns the list unchanged on success."""
    bad: list[tuple[str, str]] = []
    for u in urls:
        reason = diagnose_url(u)
        if reason is not None:
            bad.append((u, reason))
    if bad:
        joined = "\n  - ".join(f"{u} — {why}" for u, why in bad)
        raise URLSourceError(
            f"{len(bad)} invalid URL(s) from {source}:\n  - {joined}"
        )
    return urls


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve_urls(
    *,
    cli_urls: list[str] | None = None,
    urls_file: Path | None = None,
    env: dict[str, str] | None = None,
    settings_fallback: list[str] | None = None,
) -> ResolvedURLs:
    """Pick the highest-priority non-empty source and return cleaned URLs.

    Priority:
      1. CLI args
      2. ``--urls-file``
      3. ``AUTOCODER_URLS`` env
      4. ``settings_fallback`` (typically ``[LOGIN_URL, BASE_URL]``)

    Raises :class:`URLSourceError` if the chosen source contains
    malformed URLs. Returns ``ResolvedURLs(urls=[], source="none")``
    when every source is empty — the caller decides whether the empty
    result is fatal.
    """
    if cli_urls:
        cleaned = parse_url_list("\n".join(cli_urls))
        return ResolvedURLs(urls=validate_urls(cleaned, "CLI args"), source="cli")

    if urls_file is not None:
        from_file = read_urls_file(urls_file)
        if from_file:
            return ResolvedURLs(
                urls=validate_urls(from_file, f"--urls-file {urls_file}"),
                source=f"file:{urls_file}",
            )

    from_env = read_urls_env(env)
    if from_env:
        return ResolvedURLs(
            urls=validate_urls(from_env, f"${ENV_VAR}"),
            source="env",
        )

    if settings_fallback:
        fallback = parse_url_list("\n".join(u for u in settings_fallback if u))
        if fallback:
            return ResolvedURLs(
                urls=validate_urls(fallback, "settings (LOGIN_URL/BASE_URL)"),
                source="settings",
            )

    return ResolvedURLs(urls=[], source="none")
