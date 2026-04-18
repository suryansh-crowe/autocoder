"""URL intake — discover, classify, and order the URLs to process.

Three tiny modules:

* :mod:`autocoder.intake.sources`    — resolve URLs from CLI / file / env.
* :mod:`autocoder.intake.classifier` — probe each URL in a real browser.
* :mod:`autocoder.intake.graph`      — build a dependency graph + topological order.

The classifier uses real-browser probes (no LLM tokens) so the results
are grounded in actual app behavior. Output feeds into the registry,
which then drives auth-first ordering and extraction.
"""

from autocoder.intake.classifier import classify_urls
from autocoder.intake.graph import build_dependency_graph, topological_order
from autocoder.intake.sources import (
    URLSourceError,
    diagnose_url,
    parse_url_list,
    read_urls_env,
    read_urls_file,
    resolve_urls,
    validate_urls,
)

__all__ = [
    "classify_urls",
    "build_dependency_graph",
    "topological_order",
    "URLSourceError",
    "diagnose_url",
    "parse_url_list",
    "read_urls_env",
    "read_urls_file",
    "resolve_urls",
    "validate_urls",
]
