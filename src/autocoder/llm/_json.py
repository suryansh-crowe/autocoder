"""Best-effort JSON recovery shared by every LLM backend.

Local Phi-4 14B, Azure OpenAI, OpenAI-compatible, and most hosted
chat completions occasionally return JSON with one or more of:

* markdown fences (```json ... ```),
* a prose preamble or trailing explanation,
* an unterminated string near the token limit,
* a missing closing brace.

``_try_parse_json`` walks a cheap-to-expensive recovery ladder before
returning ``None``. It is used by the Ollama and Azure OpenAI clients
so behaviour and instrumentation stay identical across backends.
"""

from __future__ import annotations

import json
import re
from typing import Any


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def try_parse_json(text: str) -> Any | None:
    """Best-effort JSON recovery — returns ``None`` if all attempts fail.

    Recovery ladder (cheapest -> most aggressive):

    1. Direct ``json.loads`` on the stripped text.
    2. Strip markdown fences (``` or ```json) and retry.
    3. Slice the outermost balanced ``{ ... }`` — respects strings
       and escapes so braces inside quoted values don't break the
       scan.
    4. If the payload has an odd number of unescaped quotes or
       missing closing braces/brackets, tentatively close them.
    """
    if not text:
        return None
    stripped = text.strip()

    # 1. direct
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # 2. fences
    defenced = _FENCE_RE.sub("", stripped).strip()
    if defenced and defenced != stripped:
        try:
            return json.loads(defenced)
        except json.JSONDecodeError:
            pass

    candidate = defenced or stripped

    # 3. outermost balanced object
    start = candidate.find("{")
    if start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(candidate)):
            ch = candidate[i]
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(candidate[start : i + 1])
                    except json.JSONDecodeError:
                        break

    # 4. unterminated string / missing closers
    slice_start = start if start != -1 else 0
    fragment = candidate[slice_start:]
    quotes = 0
    esc = False
    for ch in fragment:
        if esc:
            esc = False
            continue
        if ch == "\\":
            esc = True
            continue
        if ch == '"':
            quotes += 1
    patched = fragment
    if quotes % 2 == 1:
        patched = patched + '"'
    opens = patched.count("{") - patched.count("}")
    if opens > 0:
        patched = patched + ("}" * opens)
    opens_sq = patched.count("[") - patched.count("]")
    if opens_sq > 0:
        patched = patched + ("]" * opens_sq)
    if patched != fragment:
        try:
            return json.loads(patched)
        except json.JSONDecodeError:
            return None
    return None


_STRICT_SUFFIX = (
    "\n\nSTRICT OUTPUT REQUIREMENTS:\n"
    "- Respond with exactly ONE JSON object and nothing else.\n"
    "- Do not wrap the response in markdown or prose.\n"
    "- Close every string and every brace.\n"
    "- Keep total output short enough to complete."
)


def strict_system_suffix() -> str:
    """Appended to the system prompt on retry. Works for every backend."""
    return _STRICT_SUFFIX
