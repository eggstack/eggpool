"""Offline contract tests for the opt-in Q007 live-provider harness."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.qualification_live_provider import (
    CASES,
    DEFAULT_FIXTURE,
    MAX_REQUESTS,
    RequestCase,
    bounded,
    main,
    run_qualification,
    validate_request_plan,
)


def test_q007_requires_explicit_opt_in(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="exactly one"):
        main(["--binary", str(tmp_path / "candidate")])


def test_q007_refuses_request_plan_above_frozen_budget() -> None:
    oversized = tuple(CASES) + tuple(
        RequestCase(
            f"extra-{index}",
            "model",
            "messages",
            "anthropic_messages",
            False,
        )
        for index in range(MAX_REQUESTS)
    )
    with pytest.raises(RuntimeError, match="maximum"):
        validate_request_plan(oversized)


def test_q007_redacts_secrets_and_proxy_urls() -> None:
    value = bounded(
        "Bearer sk-live-secret proxy=https://user:pass@example.test/x",
        ("sk-live-secret",),
    )
    assert "sk-live-secret" not in value
    assert "user:pass" not in value
    assert "<redacted>" in value


def test_q007_loopback_fake_emits_secret_free_evidence(tmp_path: Path) -> None:
    candidate = Path(__file__).parents[2] / "rust/target/debug/eggpool"
    if not candidate.is_file():
        pytest.skip("build the Rust candidate to run the loopback qualification")
    report = run_qualification(
        binary=candidate,
        live=False,
    )
    assert report["status"] == "pass"
    assert report["offline_fake"] is True
    assert report["durable"]["completed_requests"] == len(CASES)
    assert all(cell["raw_response_body_retained"] is False for cell in report["cells"])
    assert "q007-provider-key" not in json.dumps(report)
    assert "sk-" not in json.dumps(report)


def test_q007_missing_live_credential_is_blocked(tmp_path: Path) -> None:
    report = run_qualification(
        binary=tmp_path / "candidate",
        live=True,
        provider_key_env="Q007_MISSING_KEY",
    )
    assert report["status"] == "blocked"
    assert "Q007_MISSING_KEY" in report["reason"]


def test_q007_fixture_is_loopback_safe_and_uses_env_credential() -> None:
    text = DEFAULT_FIXTURE.read_text(encoding="utf-8")
    assert "__Q007_UPSTREAM__" in text
    assert "Q007_PROVIDER_API_KEY" in text
    assert "https://" not in text
    assert 'api_key = "q007-server-key"' in text
