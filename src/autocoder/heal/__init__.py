"""Heal stage — fill `NotImplementedError` step stubs via the local LLM.

The renderer leaves an explicit `raise NotImplementedError("Implement
step: …")` whenever the feature plan produced a step text that could
not be safely bound to a POM method (assertions, navigation, anything
the LLM described in plain English without naming a method). That is
*intentional* — the suite never silently passes.

After the user runs the suite once and sees what's broken, this stage
asks the LLM, per-stub, for a single Python statement that implements
the step. Suggestions are AST-validated against the POM's real method
list, so hallucinated names cannot reach the file. Cached on disk by
``(slug, step_text, page_fingerprint)`` so reruns spend zero tokens.

Public API:

    heal_steps(settings, opts)  →  list[HealResult]
"""

from autocoder.heal.runner import HealOptions, HealResult, heal_steps
from autocoder.heal.scanner import StubInfo, find_stubs_in_dir, find_stubs_in_file

__all__ = [
    "heal_steps",
    "HealOptions",
    "HealResult",
    "StubInfo",
    "find_stubs_in_dir",
    "find_stubs_in_file",
]
