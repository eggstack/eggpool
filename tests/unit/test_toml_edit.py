"""Tests for formatting-preserving TOML scalar edits."""

from __future__ import annotations

import tomllib

from eggpool.toml_edit import section_has_key, update_section_value


def test_update_accepts_assignment_without_spaces() -> None:
    result = update_section_value(
        ["[server]", "port=8080"],
        "server",
        "port",
        "9090",
    )

    assert result.key_found
    assert result.lines == ["[server]", "port = 9090"]


def test_append_missing_section() -> None:
    result = update_section_value(
        ["[server]", "port = 8080"],
        "dashboard",
        "public",
        "false",
        insert_missing_key=True,
        append_missing_section=True,
    )

    parsed = tomllib.loads("\n".join(result.lines))
    assert parsed["server"]["port"] == 8080
    assert parsed["dashboard"]["public"] is False


def test_similarly_prefixed_key_is_not_replaced() -> None:
    result = update_section_value(
        ["[server]", 'api_key_environment = "TOKEN"'],
        "server",
        "api_key",
        '"secret"',
        insert_missing_key=True,
    )

    assert result.lines == [
        "[server]",
        'api_key = "secret"',
        'api_key_environment = "TOKEN"',
    ]


def test_array_of_tables_section_is_recognized() -> None:
    """An [[section]] header must not be reported as a missing section."""
    result = update_section_value(
        ["[[providers]]", 'name = "a"', "port = 8080"],
        "providers",
        "port",
        "9090",
    )

    assert result.section_found
    assert result.lines == ["[[providers]]", 'name = "a"', "port = 9090"]


def test_array_of_tables_missing_key_is_inserted_not_duplicated() -> None:
    result = update_section_value(
        ["[server]", "port = 8080", "[[items]]", 'name = "x"'],
        "items",
        "count",
        "3",
        insert_missing_key=True,
        append_missing_section=True,
    )

    assert result.section_found
    # Key inserted after the existing header; no duplicate section appended.
    assert result.lines == [
        "[server]",
        "port = 8080",
        "[[items]]",
        "count = 3",
        'name = "x"',
    ]


def test_multiline_basic_string_value_is_replaced_wholesale() -> None:
    original = [
        "[server]",
        'api_key = """abc',
        "def = looks like an assignment",
        '"""',
        "port = 8080",
    ]
    result = update_section_value(original, "server", "api_key", '"secret"')

    assert result.key_found
    parsed = tomllib.loads("\n".join(result.lines))
    assert parsed["server"]["api_key"] == "secret"
    assert parsed["server"]["port"] == 8080


def test_multiline_array_value_is_replaced_wholesale() -> None:
    original = [
        "[server]",
        "cors_origins = [",
        '  "https://a.example",',
        '  "https://b.example",',
        "]",
        "port = 8080",
    ]
    result = update_section_value(
        original, "server", "cors_origins", '["https://c.example"]'
    )

    assert result.key_found
    parsed = tomllib.loads("\n".join(result.lines))
    assert parsed["server"]["cors_origins"] == ["https://c.example"]
    assert parsed["server"]["port"] == 8080


def test_multiline_string_inner_assignment_does_not_match_key() -> None:
    original = [
        "[agent]",
        'prompt = """',
        "api_key = placeholder inside text",
        '"""',
    ]
    assert not section_has_key(original, "server", "api_key")

    result = update_section_value(
        original, "server", "api_key", '"secret"', append_missing_section=True
    )
    parsed = tomllib.loads("\n".join(result.lines))
    assert parsed["server"]["api_key"] == "secret"
    assert "placeholder inside text" in parsed["agent"]["prompt"]


def test_commented_section_headers_are_recognized() -> None:
    """A header with an inline comment is valid TOML and must open/close
    sections like a plain header."""
    lines = [
        "[server] # bind address",
        'host = "127.0.0.1"',
        "[quota] # limits",
        "requests_per_minute = 60",
    ]
    assert section_has_key(lines, "server", "host")
    assert section_has_key(lines, "quota", "requests_per_minute")
    # A commented header of another section closes the current one.
    assert not section_has_key(lines, "server", "requests_per_minute")
    assert not section_has_key(lines, "quota", "host")
    # A commented-out header is not a section boundary.
    assert not section_has_key(["# [server]", "port = 1"], "server", "port")


def test_update_with_commented_header_does_not_duplicate_section() -> None:
    """The audit reproduction: updating a key in a config whose headers
    carry inline comments must not report the section missing (which
    would append a duplicate table and corrupt the document)."""
    original = [
        "[server] # bind address",
        'host = "127.0.0.1"',
        "[quota] # limits",
        "requests_per_minute = 60",
    ]
    result = update_section_value(
        original,
        "quota",
        "requests_per_minute",
        "120",
        insert_missing_key=True,
        append_missing_section=True,
    )

    assert result.section_found
    assert result.key_found
    text = "\n".join(result.lines)
    parsed = tomllib.loads(text)  # duplicate [quota] would raise here
    assert parsed["quota"]["requests_per_minute"] == 120
    assert parsed["server"]["host"] == "127.0.0.1"


def test_missing_key_is_inserted_after_commented_header() -> None:
    original = ["[server] # bind address", 'host = "127.0.0.1"']
    result = update_section_value(
        original,
        "server",
        "port",
        "8080",
        insert_missing_key=True,
        append_missing_section=True,
    )

    assert result.section_found
    # The key was absent, so it was inserted after the (commented) header.
    assert result.lines[0] == "[server] # bind address"
    assert result.lines[1] == "port = 8080"
    parsed = tomllib.loads("\n".join(result.lines))
    assert parsed["server"]["port"] == 8080
