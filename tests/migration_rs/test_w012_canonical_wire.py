"""Python-side snapshot checks for the W012 differential oracle."""

from __future__ import annotations

import json

from tests.migration_rs.w012_canonical_wire import (
    FIXTURE_PATH,
    build_w012_observations,
    observation_json,
)


def test_w012_observations_are_repeatable_and_match_snapshot() -> None:
    first = build_w012_observations()
    second = build_w012_observations()
    assert first == second
    assert observation_json() == json.dumps(
        second, sort_keys=True, separators=(",", ":")
    )
    assert json.loads(FIXTURE_PATH.read_text(encoding="utf-8")) == second


def test_w012_has_the_complete_differential_matrix_and_required_regressions() -> None:
    observations = build_w012_observations()
    assert (
        sum(len(case["profiles"]) for case in observations["requests"].values()) == 15
    )
    assert (
        sum(len(case["clients"]) for case in observations["responses"].values()) == 15
    )
    assert (
        sum(len(case["client_frames"]) for case in observations["streams"].values())
        == 15
    )
    assert observations["w011_regression_fixture"] == "w011-sse-utf8-observations.json"
    assert observations["presence_cases"]["explicit_zero_and_null"]["canonical"]
    assert {case["outcome"] for case in observations["negative_cases"].values()} == {
        "malformed",
        "provider_error",
        "premature_eof_no_success",
    }
    assert set(observations["loss_cases"]) == {"warn", "reject"}
    for stream in observations["streams"].values():
        assert stream["chunk_invariant"] is True
        assert stream["terminal"]["saw_terminal_event"] is True
