"""W001 contract and deterministic fixture-freeze tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eggpool.proxy.sse import SSEDecoder
from eggpool.transcoder import LOSS_WARNING_KINDS
from eggpool.wire.registry import load_wire_registry
from tests.migration_rs.canonical_wire_fixtures import (
    MATRIX_PATH,
    PUBLIC_SURFACES,
    WIRE_PROFILES,
    build_observation_bundle,
    observation_json,
)

ROOT = Path(__file__).parents[2]
OBSERVATION_PATH = (
    ROOT
    / "migration-rs"
    / "fixtures"
    / "canonical-wire"
    / "w001-python-observations.json"
)
SSE_MATRIX_PATH = (
    ROOT
    / "migration-rs"
    / "fixtures"
    / "canonical-wire"
    / "w001-sse-fixture-inventory.json"
)


def _matrix() -> dict[str, object]:
    return json.loads(MATRIX_PATH.read_text(encoding="utf-8"))


def _committed_projection(bundle: dict[str, object]) -> dict[str, object]:
    requests = bundle["requests"]
    assert isinstance(requests, dict)
    request_projection = {
        name: {
            key: value
            for key, value in case.items()
            if key in {"raw_body_bytes", "parsed_model", "streaming"}
        }
        for name, case in requests.items()
        if isinstance(case, dict) and name in PUBLIC_SURFACES
    }
    responses = bundle["responses"]
    assert isinstance(responses, dict)
    response_projection = {
        name: {
            "kinds": [block["kind"] for block in case["canonical"]["output"]],
            "finish": case["canonical"]["finish_reason"],
        }
        for name, case in responses.items()
    }
    streams = bundle["streams"]
    assert isinstance(streams, dict)
    stream_projection = {
        name: {
            "event_types": case["event_types"],
            "chunk_invariant": case["chunk_invariant"],
            "terminal_kind": case["terminal"]["terminal_kind"],
            "saw_terminal_event": case["terminal"]["saw_terminal_event"],
            "premature_eof_terminal": case["premature_eof_terminal"],
            "oversized_unterminated": case["oversized_unterminated"],
        }
        for name, case in streams.items()
    }
    usage = bundle["usage"]
    assert isinstance(usage, dict)
    usage_projection = {
        name: {
            key: value
            for key, value in case.items()
            if key
            in {
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "cached_input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "cache_write_input_tokens",
                "cache_counter_status",
            }
        }
        for name, case in usage.items()
    }
    return {
        "schema_version": "m6-canonical-wire-w001-observations/v1",
        "wire_profile_inventory": bundle["wire_profile_inventory"],
        "requests": request_projection,
        "responses": response_projection,
        "streams": stream_projection,
        "usage": usage_projection,
        "limits": bundle["limits"],
    }


def test_w001_observation_generation_is_repeatable_and_snapshot_matches() -> None:
    first = build_observation_bundle()
    second = build_observation_bundle()
    assert observation_json() == json.dumps(
        second, sort_keys=True, separators=(",", ":")
    )
    expected = json.loads(OBSERVATION_PATH.read_text(encoding="utf-8"))
    assert _committed_projection(first) == expected


def test_every_built_in_profile_is_in_static_inventory() -> None:
    matrix = _matrix()
    registry = load_wire_registry()
    expected = matrix["wire_profiles"]
    assert isinstance(expected, dict)
    assert set(registry.profiles) == set(WIRE_PROFILES) == set(expected)
    for name, definition in registry.profiles.items():
        assert expected[name]["request_codec"] == definition.request_codec
        assert expected[name]["response_codec"] == definition.response_codec
        assert expected[name]["stream_codec"] == definition.stream_codec


def test_public_surfaces_and_all_wire_families_have_finite_coverage() -> None:
    bundle = build_observation_bundle()
    assert set(bundle["requests"]) >= set(PUBLIC_SURFACES)
    assert set(bundle["responses"]) == set(WIRE_PROFILES)
    assert set(bundle["streams"]) == set(WIRE_PROFILES)
    assert "gemini_generate_content" in bundle["streams"]
    assert all(bundle["streams"][name]["chunk_invariant"] for name in WIRE_PROFILES)


def test_sse_fixture_inventory_covers_native_grammar_and_terminal_evidence() -> None:
    inventory = json.loads(SSE_MATRIX_PATH.read_text(encoding="utf-8"))
    assert set(inventory["profiles"]) == set(WIRE_PROFILES)
    assert inventory["all_single_byte_split"] is True
    assert inventory["line_endings"] == ["LF", "CRLF"]
    assert set(inventory["terminal_evidence"]) >= {
        "openai_done",
        "responses_completed",
        "anthropic_message_stop",
        "gemini_completed",
    }


def test_sse_grammar_probe_preserves_multiline_and_ignored_fields() -> None:
    decoder = SSEDecoder()
    frames = decoder.feed(
        b": comment\n"
        b"event: fixture\n"
        b"id: fixture-id\n"
        b"retry: ignored\n"
        b"data: first\n"
        b"data: second\n\n"
    )
    assert len(frames) == 1
    frame = frames[0].frame
    assert frame.event == "fixture"
    assert frame.data == "first\nsecond"
    assert frame.is_comment_only is False
    assert ("retry", "ignored") in frame.fields


def test_usage_zero_missing_and_cache_shapes_are_not_collapsed() -> None:
    usage = build_observation_bundle()["usage"]
    assert usage["explicit_zero"]["input_tokens"] == 0
    assert usage["explicit_zero"]["cache_read_input_tokens"] == 0
    assert usage["missing_fields"]["output_tokens"] is None
    assert usage["missing_usage"]["cache_counter_status"] == "unknown_format"
    assert usage["anthropic_reported_cache"]["cache_creation_input_tokens"] == 1


def test_stable_loss_and_error_codes_are_explicit() -> None:
    bundle = build_observation_bundle()
    matrix = _matrix()
    assert set(matrix["loss_and_error_reason_codes"]) <= set(
        bundle["stable_reason_codes"]
    )
    assert {
        "exact_native",
        "compatible_warning",
        "approved_semantic_loss",
        "unsupported_loss_rejected",
    } <= set(bundle["stable_reason_codes"])
    assert {
        "malformed_client_request",
        "malformed_provider_response",
        "incomplete_stream_terminal_evidence",
    } <= set(bundle["stable_reason_codes"])
    assert "document_media_type_unsupported" in LOSS_WARNING_KINDS
    assert "cache_control_unsupported_by_target_protocol" in LOSS_WARNING_KINDS


def test_request_invalid_cases_and_resource_edges_are_present() -> None:
    bundle = build_observation_bundle()
    invalid = bundle["requests"]["invalid"]
    assert {case["case"] for case in invalid} == {
        "malformed_json",
        "wrong_top_level",
        "missing_model",
        "blank_model",
    }
    limits = bundle["limits"]
    assert limits["max_request_body_bytes"] == 10 * 1024 * 1024
    assert limits["max_sse_frame_bytes"] == 64 * 1024
    assert limits["empty_input_tokens"] == 1000
    assert limits["reservation_large_body_tokens"] == 128000


def test_observation_and_fixture_inputs_are_secret_safe() -> None:
    matrix = _matrix()
    rendered = observation_json()
    for marker in matrix["secret_markers_forbidden"]:
        assert marker not in rendered
    assert "fixture raw content" not in rendered
    assert "Authorization: Bearer" not in rendered


def test_m7_boundary_is_documented_but_not_in_observation_runtime() -> None:
    bundle = build_observation_bundle()
    assert bundle["m7_boundary"] == _matrix()["m7_boundary"]
    assert not any("resolver" in module for module in bundle["oracle_modules"])


@pytest.mark.parametrize("profile", WIRE_PROFILES)
def test_each_profile_has_native_terminal_and_premature_eof_fixture(
    profile: str,
) -> None:
    stream = build_observation_bundle()["streams"][profile]
    assert stream["terminal"]["saw_terminal_event"] is True
    assert stream["terminal"]["terminal_kind"] is not None
    assert stream["premature_eof_terminal"]["saw_terminal_event"] is False
