"""Centralised structured logger.

Two sinks:

* **Console** (stderr, coloured) — for the human running the script.
* **`manifest/logs/<YYYYMMDD>-<HHMMSS>-<cmd>.log`** — one fresh
  newline-delimited JSON file per ``autocoder ...`` invocation. Each
  record carries ``ts``, ``level``, ``event``, plus whatever
  ``key=value`` fields the caller supplied.

Levels (lowest to highest):

    debug → info / ok → warn → error

Set the floor with the ``LOG_LEVEL`` env var (``debug``, ``info``,
``warn``, ``error``). Default is ``info``. ``ok`` shares ``info``'s
threshold and just renders green.

Sensitive values rule
---------------------

Never pass credential **values** as log fields. Only pass:

* the env var **name** (``username_env="LOGIN_USERNAME"``), or
* a presence boolean (``username_present=True``).

Use :func:`safe_url` to strip query strings + fragments from URLs
before logging — they may contain bearer tokens or one-time codes.

Use :func:`llm_call` whenever the LLM is invoked so token accounting
stays uniform across stages.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit, urlunsplit

from rich.console import Console


_LEVELS: dict[str, int] = {"debug": 0, "info": 1, "ok": 1, "warn": 2, "error": 3}

_console: Console | None = None
_file_handle: TextIO | None = None
_active_log_path: Path | None = None
_min_level: int = _LEVELS["info"]


def init(
    log_target: Path | None = None,
    level: str | None = None,
    *,
    command: str | None = None,
    force_reopen: bool = False,
) -> None:
    """Configure the console + per-invocation log file.

    ``log_target`` may be:

    * a **directory** — a fresh per-invocation file is opened inside
      it, named ``<YYYYMMDD>-<HHMMSS>-<command>.log`` (or
      ``...-run.log`` when ``command`` is omitted). Collisions get
      a ``-2`` / ``-3`` / ... suffix so two invocations in the same
      second never share a file.
    * a **file path** — opened in append mode (legacy behaviour;
      kept so callers that explicitly want a single growing file
      can still use it).
    * ``None`` — console only.

    Idempotent within a process: the **first** call that establishes
    a file handle wins. Later calls only refresh the level threshold,
    so the orchestrator's `init` after the CLI's `init` doesn't open
    a second file — unless ``force_reopen=True``, in which case the
    existing handle is closed and a fresh one is opened at the new
    ``log_target``. ``force_reopen`` is used by ``run_generate`` and
    its sibling entry points once they rescope ``settings`` to the
    per-run manifest folder, so the log lands next to the artefacts
    it describes rather than in the root-level log folder the CLI
    pointed at first.

    Always safe to call multiple times.
    """
    global _console, _file_handle, _active_log_path, _min_level
    _console = Console(stderr=True, highlight=False, soft_wrap=True)
    if log_target is not None:
        if force_reopen and _file_handle is not None:
            try:
                _file_handle.close()
            except Exception:  # noqa: BLE001
                pass
            _file_handle = None
            _active_log_path = None
        if _file_handle is None:
            path = _resolve_log_path(log_target, command)
            path.parent.mkdir(parents=True, exist_ok=True)
            _file_handle = path.open("a", encoding="utf-8")
            _active_log_path = path
    chosen = (level or os.environ.get("LOG_LEVEL", "info")).strip().lower()
    _min_level = _LEVELS.get(chosen, _LEVELS["info"])


def _resolve_log_path(target: Path, command: str | None) -> Path:
    """Decide the actual file path for ``target``.

    Directory targets get a timestamped per-invocation filename;
    file targets are returned as-is.
    """
    treat_as_dir = target.is_dir() or (not target.exists() and not target.suffix)
    if not treat_as_dir:
        return target
    target.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    cmd = command or "run"
    base = f"{ts}-{cmd}"
    candidate = target / f"{base}.log"
    n = 2
    while candidate.exists():
        candidate = target / f"{base}-{n}.log"
        n += 1
    return candidate


def active_log_path() -> Path | None:
    """Path of the file the logger is currently writing to (or None)."""
    return _active_log_path


def _console_or_default() -> Console:
    global _console
    if _console is None:
        _console = Console(stderr=True, highlight=False, soft_wrap=True)
    return _console


def _emit(level: str, event: str, **fields: Any) -> None:
    if _LEVELS.get(level, 1) < _min_level:
        return
    line = {"ts": round(time.time(), 3), "level": level, "event": event, **fields}
    if _file_handle is not None:
        try:
            _file_handle.write(json.dumps(line, default=str, ensure_ascii=False) + "\n")
            _file_handle.flush()
        except Exception:
            # File logging must never crash the orchestrator.
            pass
    pretty = " ".join(f"{k}={v}" for k, v in fields.items())
    style = {
        "info": "cyan",
        "warn": "yellow",
        "error": "red",
        "ok": "green",
        "debug": "dim",
    }.get(level, "white")
    _console_or_default().print(f"[{style}]{level:>5}[/] {event} {pretty}")


# ---------------------------------------------------------------------------
# Public level helpers
# ---------------------------------------------------------------------------


def debug(event: str, **fields: Any) -> None:
    """Fine-grained internal trace (selector picks, per-element decisions)."""
    _emit("debug", event, **fields)


def info(event: str, **fields: Any) -> None:
    """Stage transition or a single decision the human cares about."""
    _emit("info", event, **fields)


def ok(event: str, **fields: Any) -> None:
    """A stage finished successfully (renders green)."""
    _emit("ok", event, **fields)


def warn(event: str, **fields: Any) -> None:
    """Something is wrong but the run continues."""
    _emit("warn", event, **fields)


def error(event: str, **fields: Any) -> None:
    """The current stage / URL failed; run may continue with others."""
    _emit("error", event, **fields)


def die(event: str, **fields: Any) -> None:
    """Log an error and exit the process. Use sparingly."""
    _emit("error", event, **fields)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Specialised helpers
# ---------------------------------------------------------------------------


def llm_call(
    *,
    model: str,
    purpose: str,
    in_tokens: int,
    out_tokens: int,
    duration_s: float,
    cached: bool = False,
    **extra: Any,
) -> None:
    """Uniform record for every LLM invocation.

    ``cached=True`` means the call was satisfied from the on-disk
    plan cache (no network traffic, no token spend).
    """
    info(
        "llm_call",
        model=model,
        purpose=purpose,
        in_tokens=in_tokens,
        out_tokens=out_tokens,
        total_tokens=in_tokens + out_tokens,
        duration=f"{duration_s:.2f}s",
        cached=cached,
        **extra,
    )


def safe_url(url: str | None) -> str:
    """Return ``url`` with anything that may carry a secret stripped out.

    Three shapes are redacted:

    * ``userinfo`` (``user:password@``) — Basic-Auth credentials
      embedded in the URL. Removed entirely; the URL is rebuilt from
      ``hostname`` + ``port`` so credentials cannot leak via the
      ``netloc`` field.
    * **query string** — most common place for one-time tokens,
      session ids, and OAuth codes.
    * **fragment** — sometimes used to carry tokens by SPAs.

    IPv6 hosts are re-bracketed so the rebuilt URL is RFC-3986 valid.
    Returns ``""`` for falsy input and ``"<unparseable-url>"`` if
    parsing raises.
    """
    if not url:
        return ""
    try:
        p = urlsplit(url)
        host = p.hostname or ""
        # Re-bracket IPv6 literals (urlparse strips the brackets in .hostname).
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = host
        if p.port is not None:
            netloc = f"{host}:{p.port}"
        return urlunsplit((p.scheme, netloc, p.path, "", ""))
    except Exception:
        return "<unparseable-url>"


def stage(name: str, **fields: Any) -> None:
    """Mark a stage boundary. Helpful when scanning the run log."""
    info(f"stage:{name}", **fields)
