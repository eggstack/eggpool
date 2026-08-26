"""Small, formatting-preserving edits for scalar TOML section values."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast


@dataclass(frozen=True)
class TomlEditResult:
    """Result of updating one scalar key in a TOML section."""

    lines: list[str]
    section_found: bool
    key_found: bool


def render_toml_string(value: str) -> str:
    """Render a string as a TOML-compatible basic string."""
    return json.dumps(value, ensure_ascii=False)


def render_toml_value(value: object) -> str:
    """Render supported Python scalar and container values as TOML."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, str):
        return render_toml_string(value)
    if isinstance(value, list):
        items = cast("list[object]", value)
        return "[" + ", ".join(render_toml_value(item) for item in items) + "]"
    if isinstance(value, dict):
        mapping = cast("dict[object, object]", value)
        fields = (
            f"{render_toml_string(str(key))} = {render_toml_value(item)}"
            for key, item in mapping.items()
        )
        return "{ " + ", ".join(fields) + " }"
    raise TypeError(f"Unsupported TOML value type: {type(value).__name__}")


def _section_header_variants(section: str) -> tuple[str, str]:
    """Return the plain-table and array-of-tables header forms."""
    return (f"[{section}]", f"[[{section}]]")


def section_has_key(lines: list[str], section: str, key: str) -> bool:
    """Return whether an exact key exists in the requested TOML section.

    Both ``[section]`` tables and ``[[section]]`` array-of-tables
    elements are recognized. Lines that continue a multiline value are
    skipped so a ``key =`` shape inside a multiline string is never
    mistaken for an assignment.
    """
    headers = _section_header_variants(section)
    in_section = False
    index = 0
    total = len(lines)
    while index < total:
        stripped = lines[index].strip()
        index += 1
        if _matches_header(stripped, headers):
            in_section = True
            continue
        if _is_section_header(stripped):
            in_section = False
            continue
        line_key = _line_key(stripped)
        if line_key is None:
            continue
        if in_section and line_key == key:
            return True
        index += _value_continuation_lines(_line_value(stripped), lines[index:])
    return False


def update_section_value(
    lines: list[str],
    section: str,
    key: str,
    rendered_value: str,
    *,
    insert_missing_key: bool = False,
    append_missing_section: bool = False,
) -> TomlEditResult:
    """Update one scalar value while preserving unrelated TOML text.

    ``rendered_value`` must already be valid TOML. Missing keys can be inserted
    immediately after an existing section header. Missing sections can
    optionally be appended to the document. Both ``[section]`` tables
    and ``[[section]]`` array-of-tables elements are recognized as the
    target section so an AoT section is never misreported as missing
    (which would append a duplicate header).
    """
    output: list[str] = []
    in_section = False
    section_found = False
    key_found = False
    headers = _section_header_variants(section)

    index = 0
    total = len(lines)
    while index < total:
        line = lines[index]
        stripped = line.strip()
        index += 1
        if _matches_header(stripped, headers):
            in_section = True
            section_found = True
            output.append(line)
            continue
        if _is_section_header(stripped):
            in_section = False
        if in_section and _line_key(stripped) == key:
            output.append(f"{key} = {rendered_value}")
            key_found = True
            # Consume the continuation lines of the old value so a
            # multiline string/array is fully replaced, not spliced.
            index += _value_continuation_lines(_line_value(stripped), lines[index:])
            continue
        output.append(line)

    if section_found and not key_found and insert_missing_key:
        header_index = next(
            index
            for index, line in enumerate(output)
            if _matches_header(line.strip(), headers)
        )
        output.insert(header_index + 1, f"{key} = {rendered_value}")
    elif not section_found and append_missing_section:
        if output and output[-1].strip():
            output.append("")
        output.extend((f"[{section}]", f"{key} = {rendered_value}"))

    return TomlEditResult(
        lines=output,
        section_found=section_found,
        key_found=key_found,
    )


def _strip_header_comment(stripped_line: str) -> str:
    """Return *stripped_line* with any trailing TOML comment removed."""
    return stripped_line.split("#", 1)[0].rstrip()


def _matches_header(stripped_line: str, headers: tuple[str, str]) -> bool:
    """Return whether *stripped_line* is one of *headers*.

    A trailing comment after the closing bracket is tolerated so a
    header written as ``[section] # comment`` still matches.
    """
    return stripped_line in headers or _strip_header_comment(stripped_line) in headers


def _is_section_header(stripped_line: str) -> bool:
    """Return whether a stripped line is a TOML table header.

    A trailing comment after the closing bracket is tolerated so
    ``[other] # comment`` still closes the current section.
    """
    line = _strip_header_comment(stripped_line)
    return line.startswith("[") and line.endswith("]")


def _line_key(stripped_line: str) -> str | None:
    """Return the key before ``=`` for a scalar assignment line."""
    key, separator, _value = stripped_line.partition("=")
    if not separator:
        return None
    return key.strip()


def _line_value(stripped_line: str) -> str:
    """Return the value text after ``=`` for an assignment line."""
    _key, _separator, value = stripped_line.partition("=")
    return value.strip()


def _value_continuation_lines(value_text: str, followers: list[str]) -> int:
    """Count how many ``followers`` continue an unterminated TOML value.

    A value is unterminated when a string delimiter or an array/inline
    table opened on the assignment line (or a continuation line) has not
    closed yet. This keeps multiline strings and arrays from being
    spliced by single-line replacement.
    """
    in_basic = False
    in_literal = False
    in_triple_basic = False
    in_triple_literal = False
    depth = 0

    def scan(text: str) -> None:
        nonlocal in_basic, in_literal, in_triple_basic, in_triple_literal, depth
        position = 0
        length = len(text)
        while position < length:
            if in_triple_basic or in_triple_literal:
                delimiter = '"""' if in_triple_basic else "'''"
                closing = text.find(delimiter, position)
                if closing == -1:
                    return
                position = closing + 3
                in_triple_basic = in_triple_literal = False
                continue
            if in_basic:
                if text[position] == "\\":
                    position += 2
                    continue
                if text[position] == '"':
                    in_basic = False
                position += 1
                continue
            if in_literal:
                if text[position] == "'":
                    in_literal = False
                position += 1
                continue
            char = text[position]
            if char == "#":
                return
            if text.startswith('"""', position):
                in_triple_basic = True
                position += 3
                continue
            if text.startswith("'''", position):
                in_triple_literal = True
                position += 3
                continue
            if char == '"':
                in_basic = True
            elif char == "'":
                in_literal = True
            elif char in "[{":
                depth += 1
            elif char in "]}":
                depth = max(0, depth - 1)
            position += 1

    scan(value_text)
    consumed = 0
    for follower in followers:
        if not (
            in_basic or in_literal or in_triple_basic or in_triple_literal or depth > 0
        ):
            break
        scan(follower)
        consumed += 1
    return consumed
