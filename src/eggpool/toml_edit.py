"""Small, formatting-preserving edits for scalar TOML section values."""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class TomlEditResult:
    """Result of updating one scalar key in a TOML section."""

    lines: list[str]
    section_found: bool
    key_found: bool


def render_toml_string(value: str) -> str:
    """Render a string as a TOML-compatible basic string."""
    return json.dumps(value, ensure_ascii=False)


def section_has_key(lines: list[str], section: str, key: str) -> bool:
    """Return whether an exact key exists in the requested TOML section."""
    in_section = False
    for line in lines:
        stripped = line.strip()
        if stripped == f"[{section}]":
            in_section = True
            continue
        if _is_section_header(stripped):
            in_section = False
            continue
        if in_section and _line_key(stripped) == key:
            return True
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
    optionally be appended to the document.
    """
    output: list[str] = []
    in_section = False
    section_found = False
    key_found = False

    for line in lines:
        stripped = line.strip()
        if stripped == f"[{section}]":
            in_section = True
            section_found = True
            output.append(line)
            continue
        if _is_section_header(stripped):
            in_section = False
        if in_section and _line_key(stripped) == key:
            output.append(f"{key} = {rendered_value}")
            key_found = True
            continue
        output.append(line)

    if section_found and not key_found and insert_missing_key:
        header_index = next(
            index for index, line in enumerate(output) if line.strip() == f"[{section}]"
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


def _is_section_header(stripped_line: str) -> bool:
    """Return whether a stripped line is a TOML table header."""
    return stripped_line.startswith("[") and stripped_line.endswith("]")


def _line_key(stripped_line: str) -> str | None:
    """Return the key before ``=`` for a scalar assignment line."""
    key, separator, _value = stripped_line.partition("=")
    if not separator:
        return None
    return key.strip()
