"""Tests for autocoder.logger.safe_url — secret-redacting URL formatter.

Three things must always be stripped before a URL is written to a log
sink:

1. The query string (most common place for one-time tokens).
2. The fragment (sometimes used by SPAs to carry tokens).
3. The userinfo segment (``user:password@``) of the netloc — it
   carries Basic-Auth credentials.

Hostname + path + port are kept, because they are useful for
diagnostics and contain no credentials. IPv6 hosts are re-bracketed
so the rebuilt URL is RFC-3986 valid.
"""

from __future__ import annotations

import pytest

from autocoder.logger import safe_url


# ---------------------------------------------------------------------------
# Stripping rules
# ---------------------------------------------------------------------------


def test_strips_query_string() -> None:
    assert safe_url("https://app.example.com/path?token=abc") == "https://app.example.com/path"


def test_strips_fragment() -> None:
    assert safe_url("https://app.example.com/path#section") == "https://app.example.com/path"


def test_strips_query_and_fragment() -> None:
    assert (
        safe_url("https://app.example.com/path?token=abc#section")
        == "https://app.example.com/path"
    )


def test_strips_userinfo_user_only() -> None:
    """REGRESSION: previously `parsed.netloc` was used verbatim, which
    leaked the user portion of `user@host` into logs."""
    assert safe_url("https://alice@app.example.com/x") == "https://app.example.com/x"


def test_strips_userinfo_user_and_password() -> None:
    """REGRESSION: previously `https://user:secret@host/p?token=abc`
    was reduced to `https://user:secret@host/p` — credentials leaked."""
    assert (
        safe_url("https://alice:hunter2@app.example.com/x?token=abc")
        == "https://app.example.com/x"
    )


def test_strips_everything_combined() -> None:
    assert (
        safe_url("https://alice:hunter2@app.example.com:8443/x/y?session=abc#code=xyz")
        == "https://app.example.com:8443/x/y"
    )


# ---------------------------------------------------------------------------
# Things we must keep
# ---------------------------------------------------------------------------


def test_keeps_path() -> None:
    assert safe_url("https://app.example.com/a/b/c") == "https://app.example.com/a/b/c"


def test_keeps_trailing_slash() -> None:
    assert safe_url("https://app.example.com/a/b/") == "https://app.example.com/a/b/"


def test_keeps_explicit_port() -> None:
    assert safe_url("https://app.example.com:8443/x") == "https://app.example.com:8443/x"


def test_keeps_subdomain() -> None:
    assert (
        safe_url("https://api.v2.app.example.com/x")
        == "https://api.v2.app.example.com/x"
    )


def test_keeps_http_scheme() -> None:
    assert safe_url("http://app.example.com/x") == "http://app.example.com/x"


# ---------------------------------------------------------------------------
# IPv6
# ---------------------------------------------------------------------------


def test_ipv6_host_is_rebracketed() -> None:
    assert safe_url("https://[2001:db8::1]/x") == "https://[2001:db8::1]/x"


def test_ipv6_host_with_port_kept() -> None:
    assert safe_url("https://[2001:db8::1]:8443/x") == "https://[2001:db8::1]:8443/x"


def test_ipv6_host_with_userinfo_strips_creds_keeps_host() -> None:
    assert (
        safe_url("https://alice:pw@[2001:db8::1]:8443/x?t=1")
        == "https://[2001:db8::1]:8443/x"
    )


# ---------------------------------------------------------------------------
# Defensive paths
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty_string() -> None:
    assert safe_url("") == ""
    assert safe_url(None) == ""


def test_garbage_input_does_not_raise() -> None:
    # urllib.parse is famously lenient, so it usually returns *something*
    # rather than raising. Either way, we never crash the caller.
    result = safe_url("not even a url")
    assert isinstance(result, str)


def test_very_long_query_string_is_dropped() -> None:
    long_q = "a=" + "x" * 10_000
    out = safe_url(f"https://app.example.com/x?{long_q}")
    assert out == "https://app.example.com/x"
    assert "x" * 100 not in out


# ---------------------------------------------------------------------------
# Cross-cutting: the redacted URL must never contain `@`, `?`, or `#`
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://alice:hunter2@app.example.com/p?token=abc#code=xyz",
        "https://alice@host.example.com/path?q=1",
        "https://api.example.com/x?fields=a,b,c",
        "https://api.example.com/x#frag",
        "https://[2001:db8::1]:443/x?token=abc#frag",
    ],
)
def test_no_secret_carrying_chars_remain(url: str) -> None:
    redacted = safe_url(url)
    assert "@" not in redacted, f"userinfo leaked in {redacted!r}"
    assert "?" not in redacted, f"query leaked in {redacted!r}"
    assert "#" not in redacted, f"fragment leaked in {redacted!r}"
