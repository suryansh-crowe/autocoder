"""Prompt registry — system prompts stored as JSON, loaded at import.

Every prompt the autocoder sends to the LLM lives as a standalone
``*.json`` file in this directory, one file per prompt. The JSON
shape is deliberately minimal so non-Python tooling can consume it
too:

    {
      "name": "pom_plan",
      "version": 1,
      "description": "what this prompt is for",
      "source_constant": "POM_SYSTEM",
      "system": "<the full system prompt as a single string>"
    }

Why JSON (not Python constants)?
--------------------------------
* Prompts are the most-iterated surface of the whole tool. Keeping
  them as data — not code — means product / QA folks can tweak them
  without touching Python.
* Version-pinning each prompt in its own file makes diffs readable
  (``git diff src/autocoder/prompts/feature_plan.json``).
* Alternate variants for experimentation are just another file.

Loader
------
Call :func:`load_system` with the prompt's ``name`` (matches the
filename stem) to retrieve the system string. The Python modules
``autocoder.llm.prompts`` and ``autocoder.heal.prompts`` do this at
module-load time and publish the result under their traditional
constant names (``POM_SYSTEM``, ``FEATURE_SYSTEM``, …) so every
call-site keeps working without import churn.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_PROMPTS_DIR = Path(__file__).resolve().parent


class PromptNotFound(RuntimeError):
    """Raised when a requested prompt name has no matching JSON file."""


@lru_cache(maxsize=None)
def load_system(name: str) -> str:
    """Return the ``system`` string for the named prompt.

    Cached — the files are read once per process. To pick up edits
    during a long-running dev session, call :func:`load_system.cache_clear`.
    """
    path = _PROMPTS_DIR / f"{name}.json"
    if not path.is_file():
        raise PromptNotFound(
            f"No prompt JSON for {name!r} at {path} — available: "
            f"{sorted(p.stem for p in _PROMPTS_DIR.glob('*.json') if p.stem != 'index')}"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PromptNotFound(f"Cannot read prompt {path}: {exc!s}") from exc
    system = payload.get("system")
    if not isinstance(system, str) or not system:
        raise PromptNotFound(
            f"Prompt {path} is missing a non-empty 'system' field"
        )
    return system


def available_names() -> list[str]:
    """Return every prompt name the loader can serve (filename stems)."""
    return sorted(
        p.stem for p in _PROMPTS_DIR.glob("*.json") if p.stem != "index"
    )
