"""Tests for autocoder.intake.sources — URL source resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from autocoder.intake.sources import (
    ENV_VAR,
    ResolvedURLs,
    URLSourceError,
    diagnose_url,
    parse_url_list,
    read_urls_env,
    read_urls_file,
    resolve_urls,
    validate_urls,
)


# ---------------------------------------------------------------------------
# parse_url_list
# ---------------------------------------------------------------------------


def test_parse_handles_none_and_blank() -> None:
    assert parse_url_list(None) == []
    assert parse_url_list("") == []
    assert parse_url_list("   \n  \n  ") == []


def test_parse_strips_whitespace_and_blanks() -> None:
    text = """
        https://a.example.com/one

           https://b.example.com/two
    """
    assert parse_url_list(text) == ["https://a.example.com/one", "https://b.example.com/two"]


def test_parse_drops_comment_lines() -> None:
    text = """
    # leading comment
    https://a.example.com
    # trailing comment
    https://b.example.com
    """
    assert parse_url_list(text) == ["https://a.example.com", "https://b.example.com"]


def test_parse_supports_comma_and_newline_mixed() -> None:
    text = "https://a.example.com,https://b.example.com\nhttps://c.example.com"
    assert parse_url_list(text) == [
        "https://a.example.com",
        "https://b.example.com",
        "https://c.example.com",
    ]


def test_parse_dedupes_preserving_order() -> None:
    text = "https://a.example.com\nhttps://b.example.com\nhttps://a.example.com"
    assert parse_url_list(text) == ["https://a.example.com", "https://b.example.com"]


# ---------------------------------------------------------------------------
# read_urls_file
# ---------------------------------------------------------------------------


def test_file_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(URLSourceError, match="not found"):
        read_urls_file(tmp_path / "nope.txt")


def test_file_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(URLSourceError, match="not a file"):
        read_urls_file(tmp_path)


def test_file_reads_and_cleans(tmp_path: Path) -> None:
    p = tmp_path / "urls.txt"
    p.write_text(
        "# header\nhttps://a.example.com\n\nhttps://b.example.com\n# trailing\n",
        encoding="utf-8",
    )
    assert read_urls_file(p) == ["https://a.example.com", "https://b.example.com"]


# ---------------------------------------------------------------------------
# read_urls_env
# ---------------------------------------------------------------------------


def test_env_missing_returns_empty() -> None:
    assert read_urls_env({}) == []


def test_env_reads_comma_separated() -> None:
    env = {ENV_VAR: "https://a.example.com,https://b.example.com"}
    assert read_urls_env(env) == ["https://a.example.com", "https://b.example.com"]


def test_env_reads_newline_separated() -> None:
    env = {ENV_VAR: "https://a.example.com\nhttps://b.example.com"}
    assert read_urls_env(env) == ["https://a.example.com", "https://b.example.com"]


# ---------------------------------------------------------------------------
# validate_urls
# ---------------------------------------------------------------------------


def test_validate_accepts_https_and_http() -> None:
    urls = ["https://a.example.com", "http://b.example.com/path"]
    assert validate_urls(urls, "test") == urls


def test_validate_rejects_missing_scheme() -> None:
    with pytest.raises(URLSourceError, match="invalid URL"):
        validate_urls(["a.example.com"], "test")


def test_validate_rejects_missing_host() -> None:
    with pytest.raises(URLSourceError, match="invalid URL"):
        validate_urls(["https://"], "test")


def test_validate_rejects_unsupported_scheme() -> None:
    with pytest.raises(URLSourceError, match="invalid URL"):
        validate_urls(["ftp://a.example.com"], "test")


def test_validate_lists_every_bad_url_in_message() -> None:
    with pytest.raises(URLSourceError) as exc:
        validate_urls(["https://ok.example.com", "bad-1", "bad-2"], "src")
    msg = str(exc.value)
    assert "src" in msg
    assert "bad-1" in msg
    assert "bad-2" in msg


# ---------------------------------------------------------------------------
# resolve_urls — priority + integration
# ---------------------------------------------------------------------------


def test_resolve_prefers_cli_over_file_and_env(tmp_path: Path) -> None:
    p = tmp_path / "urls.txt"
    p.write_text("https://from-file.example.com", encoding="utf-8")
    env = {ENV_VAR: "https://from-env.example.com"}
    result = resolve_urls(
        cli_urls=["https://from-cli.example.com"],
        urls_file=p,
        env=env,
    )
    assert result == ResolvedURLs(urls=["https://from-cli.example.com"], source="cli")


def test_resolve_falls_back_to_file_when_no_cli(tmp_path: Path) -> None:
    p = tmp_path / "urls.txt"
    p.write_text("https://from-file.example.com", encoding="utf-8")
    env = {ENV_VAR: "https://from-env.example.com"}
    result = resolve_urls(cli_urls=None, urls_file=p, env=env)
    assert result.urls == ["https://from-file.example.com"]
    assert result.source.startswith("file:")


def test_resolve_falls_back_to_env_when_no_cli_or_file() -> None:
    env = {ENV_VAR: "https://from-env.example.com"}
    result = resolve_urls(cli_urls=[], urls_file=None, env=env)
    assert result == ResolvedURLs(urls=["https://from-env.example.com"], source="env")


def test_resolve_returns_empty_source_none_when_nothing_set() -> None:
    result = resolve_urls(cli_urls=[], urls_file=None, env={})
    assert result == ResolvedURLs(urls=[], source="none")


def test_resolve_validates_chosen_source(tmp_path: Path) -> None:
    # Bad URL on CLI — should raise rather than silently fall through.
    with pytest.raises(URLSourceError, match="CLI args"):
        resolve_urls(cli_urls=["not-a-url"], urls_file=None, env={})


def test_resolve_skips_empty_file_and_uses_env(tmp_path: Path) -> None:
    p = tmp_path / "urls.txt"
    p.write_text("# only comments\n\n", encoding="utf-8")
    env = {ENV_VAR: "https://from-env.example.com"}
    result = resolve_urls(cli_urls=None, urls_file=p, env=env)
    assert result.urls == ["https://from-env.example.com"]
    assert result.source == "env"


def test_resolve_uses_settings_fallback_last() -> None:
    result = resolve_urls(
        cli_urls=None,
        urls_file=None,
        env={},
        settings_fallback=["https://app.example.com/login", "https://app.example.com"],
    )
    assert result.source == "settings"
    assert result.urls == ["https://app.example.com/login", "https://app.example.com"]


def test_resolve_settings_fallback_dropped_in_favour_of_env() -> None:
    result = resolve_urls(
        cli_urls=None,
        urls_file=None,
        env={ENV_VAR: "https://from-env.example.com"},
        settings_fallback=["https://from-settings.example.com"],
    )
    assert result.source == "env"
    assert result.urls == ["https://from-env.example.com"]


def test_resolve_settings_fallback_validates() -> None:
    with pytest.raises(URLSourceError, match="settings"):
        resolve_urls(cli_urls=None, urls_file=None, env={}, settings_fallback=["bad-url"])


def test_resolve_settings_fallback_skips_empties() -> None:
    result = resolve_urls(
        cli_urls=None, urls_file=None, env={}, settings_fallback=[None, "", "   "]
    )
    assert result == ResolvedURLs(urls=[], source="none")


# ---------------------------------------------------------------------------
# Regression: structure-aware splitting (URLs with commas in query/fragment)
# ---------------------------------------------------------------------------


def test_parse_keeps_comma_inside_query_string() -> None:
    """Regression for the original bug: query-string commas were being
    treated as URL separators, splitting one URL into garbage fragments."""
    text = "https://example.com/api?fields=name,email,role"
    assert parse_url_list(text) == [text]


def test_parse_keeps_comma_inside_fragment() -> None:
    text = "https://example.com/page#a,b,c"
    assert parse_url_list(text) == [text]


def test_parse_splits_only_at_url_boundaries_in_csv() -> None:
    text = (
        "https://a.example.com/api?fields=x,y,z,"
        "https://b.example.com/api?fields=p,q"
    )
    assert parse_url_list(text) == [
        "https://a.example.com/api?fields=x,y,z",
        "https://b.example.com/api?fields=p,q",
    ]


def test_parse_csv_with_whitespace_after_separator() -> None:
    text = "https://a.example.com,  https://b.example.com  ,https://c.example.com"
    assert parse_url_list(text) == [
        "https://a.example.com",
        "https://b.example.com",
        "https://c.example.com",
    ]


def test_parse_mixed_newlines_and_commas() -> None:
    text = (
        "https://a.example.com/api?q=1,2\n"
        "https://b.example.com/api?q=3,4,https://c.example.com\n"
    )
    # Note: 3rd URL is a comma-separated continuation of the 2nd line.
    assert parse_url_list(text) == [
        "https://a.example.com/api?q=1,2",
        "https://b.example.com/api?q=3,4",
        "https://c.example.com",
    ]


def test_parse_carriage_returns_treated_as_line_breaks() -> None:
    text = "https://a.example.com\r\nhttps://b.example.com\rhttps://c.example.com"
    assert parse_url_list(text) == [
        "https://a.example.com",
        "https://b.example.com",
        "https://c.example.com",
    ]


def test_parse_url_with_trailing_slash_preserved() -> None:
    text = "https://a.example.com/path/\nhttps://b.example.com/path"
    assert parse_url_list(text) == [
        "https://a.example.com/path/",
        "https://b.example.com/path",
    ]


def test_parse_url_with_userinfo_preserved_for_orchestrator() -> None:
    """Orchestrator may need basic-auth URLs verbatim. Stripping happens
    at log time (logger.safe_url), not at parse time."""
    text = "https://user:pw@a.example.com/x"
    assert parse_url_list(text) == [text]


# ---------------------------------------------------------------------------
# diagnose_url: per-URL reasons
# ---------------------------------------------------------------------------


def test_diagnose_accepts_https() -> None:
    assert diagnose_url("https://app.example.com/x") is None


def test_diagnose_accepts_http() -> None:
    assert diagnose_url("http://app.example.com/x") is None


def test_diagnose_accepts_url_with_query_and_fragment() -> None:
    assert diagnose_url("https://a.example.com/p?q=1,2,3#section") is None


def test_diagnose_accepts_url_with_port() -> None:
    assert diagnose_url("https://a.example.com:8443/x") is None


def test_diagnose_accepts_ipv6_host() -> None:
    assert diagnose_url("https://[2001:db8::1]:8443/x") is None


def test_diagnose_rejects_missing_scheme_with_helpful_hint() -> None:
    reason = diagnose_url("app.example.com/x")
    assert reason is not None
    assert "missing http/https scheme" in reason
    assert "https://app.example.com/x" in reason


def test_diagnose_rejects_unsupported_scheme() -> None:
    reason = diagnose_url("ftp://a.example.com")
    assert reason is not None
    assert "unsupported scheme" in reason
    assert "'ftp'" in reason


def test_diagnose_rejects_missing_host() -> None:
    reason = diagnose_url("https://")
    assert reason is not None
    assert "missing host" in reason


def test_diagnose_rejects_blank() -> None:
    assert diagnose_url("") is not None
    assert diagnose_url("   ") is not None


def test_diagnose_rejects_non_string() -> None:
    # mypy would catch this at type check time; at runtime we still degrade
    # gracefully rather than raising deep in the parser.
    assert diagnose_url(None) is not None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Validate: error message quality
# ---------------------------------------------------------------------------


def test_validate_error_includes_per_url_reason() -> None:
    with pytest.raises(URLSourceError) as exc:
        validate_urls(
            ["https://ok.example.com", "ftp://bad.example.com", "no-scheme.example.com"],
            "test source",
        )
    msg = str(exc.value)
    assert "ftp://bad.example.com" in msg
    assert "unsupported scheme" in msg
    assert "no-scheme.example.com" in msg
    assert "missing http/https scheme" in msg


# ---------------------------------------------------------------------------
# End-to-end resolver behaviour for the regression case
# ---------------------------------------------------------------------------


def test_resolve_env_with_query_comma_url_preserved() -> None:
    env = {ENV_VAR: "https://example.com/api?fields=name,email,role"}
    result = resolve_urls(cli_urls=None, urls_file=None, env=env)
    assert result.urls == ["https://example.com/api?fields=name,email,role"]
    assert result.source == "env"


def test_resolve_file_with_csv_and_query_commas(tmp_path: Path) -> None:
    p = tmp_path / "urls.txt"
    p.write_text(
        "https://a.example.com/api?fields=x,y\n"
        "https://b.example.com/api?fields=p,q,r\n",
        encoding="utf-8",
    )
    result = resolve_urls(cli_urls=None, urls_file=p, env={})
    assert result.urls == [
        "https://a.example.com/api?fields=x,y",
        "https://b.example.com/api?fields=p,q,r",
    ]


def test_resolve_cli_args_each_with_internal_commas() -> None:
    result = resolve_urls(
        cli_urls=[
            "https://a.example.com/api?fields=x,y",
            "https://b.example.com/api?fields=p,q",
        ],
        urls_file=None,
        env={},
    )
    assert result.urls == [
        "https://a.example.com/api?fields=x,y",
        "https://b.example.com/api?fields=p,q",
    ]
