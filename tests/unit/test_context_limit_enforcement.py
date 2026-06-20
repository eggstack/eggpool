"""Tests for context limit enforcement in the proxy request pipeline."""

from __future__ import annotations

import inspect
import json

import pytest

from eggpool.api.errors import anthropic_error_response, openai_error_response
from eggpool.api.proxy_request import (
    _check_context_limits,
)
from eggpool.errors import ContextLimitExceededError


class MockCatalogCache:
    def __init__(self, model_info: dict | None) -> None:
        self._model_info = model_info

    def get_model_for_provider(
        self, model_id: str, provider_id: str | None
    ) -> dict | None:
        return self._model_info


def test_request_below_context_limit_is_forwarded() -> None:
    cache = MockCatalogCache(
        {
            "effective_limits": {
                "context_tokens": 100_000,
                "enforce": True,
            }
        }
    )
    body = b'{"model":"m1","messages":[]}'
    payload = {"model": "m1", "messages": []}
    _check_context_limits(
        model_id="m1",
        provider_id=None,
        body=body,
        payload=payload,
        protocol="openai",
        catalog_cache=cache,
    )


def test_request_above_context_limit_returns_error() -> None:
    cache = MockCatalogCache(
        {
            "effective_limits": {
                "context_tokens": 100,
                "enforce": True,
            }
        }
    )
    body = b"x" * 1000
    payload = {"model": "m1", "max_tokens": 500}
    with pytest.raises(ContextLimitExceededError):
        _check_context_limits(
            model_id="m1",
            provider_id=None,
            body=body,
            payload=payload,
            protocol="openai",
            catalog_cache=cache,
        )


def test_input_specific_limit_is_enforced() -> None:
    cache = MockCatalogCache(
        {
            "effective_limits": {
                "input_tokens": 500,
                "enforce": True,
            }
        }
    )
    body = b"x" * 3000
    payload = {"model": "m1"}
    with pytest.raises(ContextLimitExceededError):
        _check_context_limits(
            model_id="m1",
            provider_id=None,
            body=body,
            payload=payload,
            protocol="openai",
            catalog_cache=cache,
        )


def test_output_specific_limit_is_enforced() -> None:
    cache = MockCatalogCache(
        {
            "effective_limits": {
                "output_tokens": 1000,
                "enforce": True,
            }
        }
    )
    body = b'{"model":"m1","messages":[]}'
    payload = {"model": "m1", "max_tokens": 2000}
    with pytest.raises(ContextLimitExceededError):
        _check_context_limits(
            model_id="m1",
            provider_id=None,
            body=body,
            payload=payload,
            protocol="openai",
            catalog_cache=cache,
        )


def test_enforcement_disabled_allows_forwarding() -> None:
    cache = MockCatalogCache(
        {
            "effective_limits": {
                "context_tokens": 100,
                "enforce": False,
            }
        }
    )
    body = b"x" * 1000
    payload = {"model": "m1"}
    _check_context_limits(
        model_id="m1",
        provider_id=None,
        body=body,
        payload=payload,
        protocol="openai",
        catalog_cache=cache,
    )


def test_provider_specific_limits_are_used() -> None:
    cache = MockCatalogCache(
        {
            "effective_limits": {
                "context_tokens": 100,
                "enforce": True,
            }
        }
    )
    body = b"x" * 1000
    payload = {"model": "m1"}
    with pytest.raises(ContextLimitExceededError):
        _check_context_limits(
            model_id="m1",
            provider_id="provider-b",
            body=body,
            payload=payload,
            protocol="openai",
            catalog_cache=cache,
        )


def test_policy_rejection_does_not_penalize_account_health() -> None:
    sig = inspect.signature(_check_context_limits)
    assert "health_manager" not in sig.parameters


def test_openai_error_envelope_is_correct() -> None:
    resp = openai_error_response(
        400, "Context limit exceeded", error_type="invalid_request_error"
    )
    assert resp.status_code == 400
    body = json.loads(resp.body)
    assert "error" in body
    error = body["error"]
    assert error["message"] == "Context limit exceeded"
    assert error["type"] == "invalid_request_error"
    assert error["code"] == "400"


def test_anthropic_error_envelope_is_correct() -> None:
    resp = anthropic_error_response(
        400, "Context limit exceeded", error_type="invalid_request_error"
    )
    assert resp.status_code == 400
    body = json.loads(resp.body)
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["message"] == "Context limit exceeded"


def test_no_request_content_is_persisted() -> None:
    src = inspect.getsource(_check_context_limits)
    for db_keyword in [
        "database",
        "db",
        "session",
        "save",
        "insert",
        "update",
        "commit",
    ]:
        assert db_keyword not in src.lower(), (
            f"_check_context_limits references persistence: {db_keyword}"
        )
