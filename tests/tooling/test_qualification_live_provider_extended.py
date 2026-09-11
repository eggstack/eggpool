"""Offline tests for the extended live-provider corrective provider profile."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from scripts.qualification_live_provider import (
    MAX_REQUESTS,
    Q011_CASES,
    Q011_FIXTURE,
    run_qualification,
    validate_request_plan,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_q011_retains_the_frozen_request_budget() -> None:
    validate_request_plan(Q011_CASES)
    assert len(Q011_CASES) == 7
    assert len(Q011_CASES) <= MAX_REQUESTS
    assert {case.provider_id for case in Q011_CASES} == {"generalcompute", "minimax"}


def test_q011_fixture_contains_no_credentials_or_unresolved_provider_secrets() -> None:
    text = Q011_FIXTURE.read_text(encoding="utf-8")
    assert "Q011_GENERALCOMPUTE_API_KEY" in text
    assert "Q011_MINIMAX_API_KEY" in text
    assert "sk-" not in text
    assert "gc_" not in text
    assert "__Q011_GENERALCOMPUTE_UPSTREAM__" in text
    assert "__Q011_MINIMAX_UPSTREAM__" in text


def test_q011_missing_any_live_credential_is_blocked(tmp_path: Path) -> None:
    report = run_qualification(
        binary=tmp_path / "candidate",
        live=True,
        profile="q011-multi",
        provider_key_env="Q011_MISSING_GENERALCOMPUTE",
        secondary_provider_key_env="Q011_MISSING_MINIMAX",
    )
    assert report["status"] == "blocked"
    assert "Q011_MISSING_GENERALCOMPUTE" in report["reason"]
    assert "Q011_MISSING_MINIMAX" in report["reason"]
    assert "sk-" not in json.dumps(report)
