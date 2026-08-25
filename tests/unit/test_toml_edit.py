"""Tests for formatting-preserving TOML scalar edits."""

from __future__ import annotations

import tomllib

from eggpool.toml_edit import update_section_value


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
