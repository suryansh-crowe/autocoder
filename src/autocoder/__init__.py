"""autocoder — URL-driven Playwright BDD test automation system.

The package is split into stages so each one can be reused, resumed, or
extended independently:

    intake/    classify URLs, build dependency graph
    extract/   drive a real browser, pull a compact UI catalog
    llm/       single-call JSON action plans against Phi-4 (Ollama)
    generate/  deterministic renderers for POMs, features, and steps
    registry/  read/write the on-disk manifest, diff and resume helpers
    cli.py     user-facing entry point
    orchestrator.py
               glues the stages together
"""

from autocoder.config import Settings, load_settings  # noqa: F401

__all__ = ["Settings", "load_settings"]
__version__ = "0.1.0"
