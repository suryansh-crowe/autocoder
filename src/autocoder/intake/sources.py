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

Validation is deliberate: a malformed URL is reported with the source
that contributed it so the user knows where to fix it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


ENV_VAR = "AUTOCODER_URLS"

_VALID_SCHEMES = {"http", "https"}


class URLSourceError(ValueError):
    """Raised when a URL source produces invalid input."""


@dataclass(frozen=True)
class ResolvedURLs:
    urls: list[str]
    source: str  # "cli", "file:<path>", "env", or "none"


def parse_url_list(text: str | None) -> list[str]:
    """Split a comma- or newline-separated URL blob into clean entries.

    Strips whitespace, drops blanks, drops ``#`` comment lines.
    Preserves order and removes duplicates.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    # Allow either commas or newlines as separators (or both interleaved).
    for raw_line in text.replace(",", "\n").splitlines():
        line = raw_line.strip()
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


def validate_urls(urls: list[str], source: str) -> list[str]:
    """Reject malformed URLs. Returns the list unchanged on success."""
    bad: list[str] = []
    for u in urls:
        parsed = urlparse(u)
        if parsed.scheme.lower() not in _VALID_SCHEMES or not parsed.netloc:
            bad.append(u)
    if bad:
        joined = "\n  - ".join(bad)
        raise URLSourceError(
            f"{len(bad)} invalid URL(s) from {source} (require http/https + host):\n  - {joined}"
        )
    return urls


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
