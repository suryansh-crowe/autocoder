"""Heal stage — rewrite failing Playwright test bodies via the LLM.

Two heal modes:

1. **Stub heal** — when the codegen prompt could not map a scenario
   statement to a safe Playwright call, the renderer emits
   ``raise NotImplementedError("Implement step: ...")`` in the test
   body. This mode asks the LLM to produce a valid list of
   Playwright statements and patches the function body in place.
2. **Failure-driven heal** — when a generated test has already run
   and failed, the runtime error is fed into a separate prompt and
   the LLM rewrites the failing test function's body.

Both modes validate suggestions statement-by-statement against the
POM's real method list and the SELECTORS element catalogue, so
hallucinated names never reach disk. Cached on ``manifest/heals/``
by ``(slug, function, fingerprint)`` so reruns spend zero tokens.

Public API:

    heal_steps(settings, opts)  →  list[HealResult]
"""

from autocoder.heal.runner import HealOptions, HealResult, heal_steps
from autocoder.heal.scanner import (
    StubInfo,
    find_function_in_file,
    find_stubs_in_dir,
    find_stubs_in_file,
)

__all__ = [
    "heal_steps",
    "HealOptions",
    "HealResult",
    "StubInfo",
    "find_stubs_in_dir",
    "find_stubs_in_file",
    "find_function_in_file",
]
