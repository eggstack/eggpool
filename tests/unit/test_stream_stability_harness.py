"""Tests for the stream-stability harness shared primitives."""

from __future__ import annotations

import httpx

from tests.helpers.stream_stability_harness import (
    ALL_CANCEL_OFFSETS,
    ALL_SCENARIOS,
    CANCEL_AFTER_FINAL_BEFORE_USAGE,
    CANCEL_AFTER_FIRST_TOKEN,
    CANCEL_BEFORE_FIRST_BYTE,
    CANCEL_MIDSTREAM,
    SCENARIO_ALIASES,
    UPSTREAM_BASE,
    normalize_scenario,
    positive_delta,
    scenario_respx_response,
    should_cancel,
    sse_chunk,
    sse_done,
    sse_usage_chunk,
)


class TestNormalizeScenario:
    def test_canonical_name_passthrough(self) -> None:
        assert normalize_scenario("happy-path") == "happy-path"
        assert normalize_scenario("slow-stream") == "slow-stream"
        assert normalize_scenario("read-timeout") == "read-timeout"

    def test_alias_resolves_to_canonical(self) -> None:
        assert normalize_scenario("slow-token-cadence") == "slow-stream"

    def test_unknown_name_passthrough(self) -> None:
        assert normalize_scenario("unknown-scenario") == "unknown-scenario"

    def test_aliases_dict_entries_match(self) -> None:
        for alias, canonical in SCENARIO_ALIASES.items():
            assert normalize_scenario(alias) == canonical


class TestAllScenarios:
    def test_contains_expected_canonical_names(self) -> None:
        expected = {
            "happy-path",
            "no-usage",
            "slow-first-byte",
            "slow-stream",
            "abrupt-upstream-close",
            "read-timeout",
            "malformed-frame",
            "connection-reset",
        }
        assert set(ALL_SCENARIOS) == expected

    def test_no_aliases_in_all_scenarios(self) -> None:
        for alias in SCENARIO_ALIASES:
            assert alias not in ALL_SCENARIOS


class TestAllCancelOffsets:
    def test_contains_expected_values(self) -> None:
        expected = {
            "before-first-byte",
            "after-first-token",
            "midstream",
            "after-final-before-usage",
        }
        assert set(ALL_CANCEL_OFFSETS) == expected


class TestSseHelpers:
    def testsse_chunk_returns_bytes(self) -> None:
        result = sse_chunk("hello")
        assert isinstance(result, bytes)
        assert b"hello" in result
        assert result.startswith(b"data: ")

    def testsse_chunk_finish_flag(self) -> None:
        result = sse_chunk("done", finish=True)
        assert b'"stop"' in result

    def testsse_usage_chunk(self) -> None:
        result = sse_usage_chunk(input_tokens=3, output_tokens=5)
        assert isinstance(result, bytes)
        assert b"prompt_tokens" in result
        assert b"completion_tokens" in result

    def testsse_done(self) -> None:
        assert sse_done() == b"data: [DONE]\n\n"


class TestShouldCancel:
    def test_not_started_returns_false(self) -> None:
        assert should_cancel(CANCEL_MIDSTREAM, 0, False) is False

    def test_before_first_byte(self) -> None:
        assert should_cancel(CANCEL_BEFORE_FIRST_BYTE, 0, True) is True
        assert should_cancel(CANCEL_BEFORE_FIRST_BYTE, 1, True) is False

    def test_after_first_token(self) -> None:
        assert should_cancel(CANCEL_AFTER_FIRST_TOKEN, 0, True) is False
        assert should_cancel(CANCEL_AFTER_FIRST_TOKEN, 1, True) is True

    def test_midstream(self) -> None:
        assert should_cancel(CANCEL_MIDSTREAM, 1, True) is False
        assert should_cancel(CANCEL_MIDSTREAM, 2, True) is True

    def test_after_final_before_usage(self) -> None:
        assert should_cancel(CANCEL_AFTER_FINAL_BEFORE_USAGE, 3, True) is False
        assert should_cancel(CANCEL_AFTER_FINAL_BEFORE_USAGE, 4, True) is True


class TestPositiveDelta:
    def test_empty_dicts(self) -> None:
        assert positive_delta({}, {}) == {}

    def test_onlypositive_deltas(self) -> None:
        baseline = {"a": 5, "b": 10}
        current = {"a": 7, "b": 10, "c": 3}
        result = positive_delta(baseline, current)
        assert result == {"a": 2, "c": 3}

    def test_negative_deltas_excluded(self) -> None:
        baseline = {"a": 10}
        current = {"a": 5}
        assert positive_delta(baseline, current) == {}


class TestScenarioRespxResponse:
    def test_all_canonical_scenarios_return_response(self) -> None:
        for scenario in ALL_SCENARIOS:
            resp = scenario_respx_response(
                scenario, chunks_per_stream=2, chunk_delay_s=0.0
            )
            assert isinstance(resp, httpx.Response)
            assert resp.status_code == 200

    def test_alias_resolves_correctly(self) -> None:
        resp = scenario_respx_response(
            "slow-token-cadence", chunks_per_stream=2, chunk_delay_s=0.0
        )
        assert isinstance(resp, httpx.Response)
        assert resp.status_code == 200

    def test_unknown_scenario_raises(self) -> None:
        try:
            scenario_respx_response(
                "nonexistent", chunks_per_stream=2, chunk_delay_s=0.0
            )
            raise AssertionError("Expected ValueError")
        except ValueError as exc:
            assert "Unknown scenario" in str(exc)


class TestUpstreamBase:
    def test_value(self) -> None:
        assert UPSTREAM_BASE == "https://test-upstream.example.com"
