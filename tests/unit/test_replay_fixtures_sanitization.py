"""Sanitization rules for Phase 11 cache/compression replay fixtures.

The replay suite must NEVER leak real secrets, real-looking emails,
provider request IDs, or natural-language paragraphs that could have
been copied from a real prompt.  This test walks every fixture file
and fails loudly when forbidden tokens appear.

The rules are intentionally conservative — false positives are
preferred over accidental leaks.  When you intentionally need to
exercise a pattern that matches a rule, adjust the rule and add a
comment explaining the change.
"""

from __future__ import annotations

import re

import pytest

from tests.helpers.cache_compression_replay import iter_fixtures

FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "openai_api_key",
        re.compile(r"sk-[A-Za-z0-9]{16,}"),
    ),
    (
        "anthropic_api_key",
        re.compile(r"sk-ant-[A-Za-z0-9]{16,}"),
    ),
    (
        "bearer_token",
        re.compile(r"Bearer\s+[A-Za-z0-9._\-]{16,}"),
    ),
    (
        "github_pat",
        re.compile(r"ghp_[A-Za-z0-9]{16,}"),
    ),
    (
        "google_api_key",
        re.compile(r"AIza[0-9A-Za-z\-_]{16,}"),
    ),
    (
        "real_email",
        re.compile(
            r"[A-Za-z0-9._%+-]+@(?:gmail|yahoo|hotmail|outlook|proton)\.(?:com|net)"
        ),
    ),
    (
        "ip_address",
        re.compile(
            r"\b(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)(?:\.(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)){3}\b"
        ),
    ),
    (
        "openai_request_id",
        re.compile(r"req_[A-Za-z0-9]{16,}"),
    ),
    (
        "openai_chatcmpl_id",
        re.compile(r"chatcmpl-[A-Za-z0-9]{16,}"),
    ),
    (
        "anthropic_request_id",
        re.compile(r"msg_[A-Za-z0-9]{16,}"),
    ),
)


@pytest.fixture(scope="module")
def cache_compression_fixtures() -> list[dict]:
    """All replay fixtures under the cache_compression root."""
    return iter_fixtures()


def _flatten_strings(node: object) -> list[str]:
    """Recursively extract every string from a JSON tree."""
    out: list[str] = []
    if isinstance(node, str):
        out.append(node)
    elif isinstance(node, dict):
        for value in node.values():
            out.extend(_flatten_strings(value))
    elif isinstance(node, list):
        for value in node:
            out.extend(_flatten_strings(value))
    return out


def _path_with_cache_control(tree: dict) -> list[str]:
    """Return ``"path=N"`` strings for any ``cache_control`` field found."""
    out: list[str] = []

    def walk(node: object, prefix: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                current = f"{prefix}.{key}" if prefix else str(key)
                if key == "cache_control":
                    out.append(f"{current}={value}")
                walk(value, current)
        elif isinstance(node, list):
            for idx, value in enumerate(node):
                walk(value, f"{prefix}[{idx}]")

    walk(tree, "")
    return out


def test_fixture_tree_does_not_contain_secrets(
    cache_compression_fixtures: list[dict],
) -> None:
    """Walk every fixture JSON and fail when any forbidden pattern matches."""
    seen: list[str] = []
    for fixture in cache_compression_fixtures:
        name = str(fixture.get("name", "<unknown>"))
        for kind, pattern in FORBIDDEN_PATTERNS:
            for value in _flatten_strings(fixture):
                if pattern.search(value):
                    seen.append(f"{name!r} matched rule {kind!r}")
        cache_control_lines = _path_with_cache_control(fixture)
        for line in cache_control_lines:
            if "type" not in line:
                seen.append(f"{name!r} cache_control without 'type' field: {line}")
    assert not seen, "Forbidden patterns found in fixtures:\n" + "\n".join(seen)


def test_fixture_names_are_unique(cache_compression_fixtures: list[dict]) -> None:
    names = [str(fixture.get("name")) for fixture in cache_compression_fixtures]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    assert not duplicates, f"Duplicate fixture names: {duplicates}"


def test_fixture_categories_are_known(cache_compression_fixtures: list[dict]) -> None:
    valid = {"openai", "anthropic", "transcode", "routing", "stats"}
    bad = [
        str(fixture.get("name"))
        for fixture in cache_compression_fixtures
        if str(fixture.get("category")) not in valid
    ]
    assert not bad, f"Fixtures with unknown category: {bad}"


def test_fixtures_use_synthetic_repeated_ids() -> None:
    """OpenAI tool_call ids and Anthropic tool_use_ids must use the synthetic prefix.

    Only strings inside the ``request`` payload are inspected -- fixture-level
    names like ``synthetic_cache_candidates`` are unrelated.
    """
    id_pattern = re.compile(r"^synthetic_(?:call|id|use)_[A-Za-z0-9_]+$")
    offenders: list[str] = []
    for fixture in iter_fixtures():
        name = str(fixture.get("name"))
        request = fixture.get("request")
        if not isinstance(request, dict):
            continue
        for value in _flatten_strings(request):
            if not value.startswith("synthetic_"):
                continue
            if not id_pattern.match(value):
                offenders.append(f"{name}: {value!r}")
    assert not offenders, "Unexpected synthetic_ prefix in fixtures:\n" + "\n".join(
        offenders
    )


def test_no_fixture_string_is_unreasonably_long() -> None:
    """Reject strings longer than 64 KB -- fixtures should never carry huge blobs."""
    limit = 64 * 1024
    offenders: list[str] = []
    for fixture in iter_fixtures():
        name = str(fixture.get("name"))
        for value in _flatten_strings(fixture):
            if len(value.encode("utf-8")) > limit:
                offenders.append(f"{name}: {len(value)} bytes")
    assert not offenders, (
        f"Fixtures contain oversized strings (>{limit} bytes):\n" + "\n".join(offenders)
    )


def test_each_fixture_has_target_protocol_for_transcode_routing() -> None:
    """Transcode and routing fixtures must declare both client and target protocols."""
    transcode_routing: list[str] = []
    for fixture in iter_fixtures():
        category = str(fixture.get("category"))
        if category not in {"transcode", "routing"}:
            continue
        name = str(fixture.get("name"))
        if not fixture.get("client_protocol") or not fixture.get("target_protocol"):
            transcode_routing.append(name)
    assert not transcode_routing, (
        "Transcode/routing fixtures missing protocols: " + ",".join(transcode_routing)
    )


def test_no_natural_language_paragraphs_in_fixtures() -> None:
    """Reject strings that look like copied real prompts.

    Only strings inside ``request`` payloads are inspected — fixture-level
    metadata like ``description`` is expected to be English prose.

    A string is flagged as a natural-language paragraph if it is longer
    than 200 characters and more than 70 % of its characters are
    lowercase letters or spaces — a strong signal of English prose.
    Strings beginning with a known sentinel are excluded (synthetic
    by construction).  Sentinel strings, hash-like tokens, and
    structured JSON are otherwise excluded because they contain mostly
    uppercase/digits/punctuation.
    """
    min_length = 200
    prose_ratio_threshold = 0.70
    sentinel_prefixes = (
        "SYSTEM_POLICY_SENTINEL",
        "TOOL_SCHEMA_SENTINEL",
        "VOLATILE_LOG_LINE",
        "STACK_TRACE_SENTINEL",
        "SYNTHETIC_BASE64_BLOB",
        "LONG_USER_INSTRUCTION",
        "LATEST_USER_SENTINEL",
        "[EggPool compression:",
    )
    offenders: list[str] = []
    for fixture in iter_fixtures():
        name = str(fixture.get("name"))
        request = fixture.get("request")
        if not isinstance(request, dict):
            continue
        for value in _flatten_strings(request):
            if len(value) < min_length:
                continue
            if any(value.startswith(p) for p in sentinel_prefixes):
                continue
            lower_count = sum(1 for c in value if c.islower() or c == " ")
            ratio = lower_count / len(value)
            if ratio >= prose_ratio_threshold:
                offenders.append(f"{name}: {len(value)} chars, prose_ratio={ratio:.2f}")
    assert not offenders, (
        "Fixtures contain natural-language paragraphs that may be real prompts:\n"
        + "\n".join(offenders)
    )
