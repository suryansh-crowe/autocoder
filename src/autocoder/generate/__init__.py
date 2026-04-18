"""Generation stage — turn a validated plan into source files.

Every renderer here is deterministic: it takes a typed plan + a typed
extraction and emits a string. Zero LLM tokens. Templates are kept
inline (small, easy to diff) rather than in a separate Jinja file.
"""

from autocoder.generate.auth_setup import render_auth_setup
from autocoder.generate.feature import render_feature
from autocoder.generate.pom import render_pom
from autocoder.generate.steps import render_steps

__all__ = ["render_pom", "render_feature", "render_steps", "render_auth_setup"]
