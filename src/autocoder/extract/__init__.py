"""Extraction stage — drive a browser, pull a compact UI catalog.

The output is a ``PageExtraction`` model per URL. Everything the LLM
later sees about the page is built from these models, so this stage is
the single place that decides what counts as automation-relevant.
"""

from autocoder.extract.browser import BrowserSession, open_session
from autocoder.extract.inspector import extract_page
from autocoder.extract.selectors import build_selector, build_selector_from_locator

__all__ = [
    "BrowserSession",
    "open_session",
    "extract_page",
    "build_selector",
    "build_selector_from_locator",
]
