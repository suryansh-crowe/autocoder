"""Generation stage — turn validated plans into source files.

The POM + feature renderers are deterministic (typed plan in, string
out, zero LLM tokens). The Playwright-script renderer consumes the
output of the 3rd LLM prompt (see :mod:`autocoder.llm.codegen`) and
re-validates every statement through the heal validator before
writing the final file.
"""

from autocoder.generate.auth_setup import render_auth_setup
from autocoder.generate.feature import render_feature
from autocoder.generate.playwright_script import render_playwright_script
from autocoder.generate.pom import render_pom

__all__ = [
    "render_pom",
    "render_feature",
    "render_playwright_script",
    "render_auth_setup",
]
