"""Tests for autocoder.intake.sources — URL source resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from autocoder.intake.sources import (
    ENV_VAR,
    ResolvedURLs,
    URLSourceError,
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
