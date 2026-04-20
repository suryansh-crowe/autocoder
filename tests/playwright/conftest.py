"""Pytest configuration for the generated pure-Playwright test suite.

Browser / storage_state / MSAL-sessionStorage / autoauth fixtures live
in ``tests/conftest.py`` and are inherited automatically — nothing
additional is needed here beyond documentation.

The autoheal plugin (``tests/support/autoheal_plugin.py``) also
applies to this directory: set ``AUTOCODER_AUTOHEAL=true`` in ``.env``
to have failing Playwright test bodies rewritten in-place by the LLM
after each run.
"""

from __future__ import annotations
