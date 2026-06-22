"""Tests for context limit enforcement in the proxy request pipeline."""

from __future__ import annotations

import inspect
import json

import pytest

from eggpool.api.errors import anthropic_error_response, openai_error_response
from eggpool.api.proxy_request import (
    _check_context_limits,
)
from eggpool.catalog.cache import ModelCatalogCache
from eggpool.catalog.limits import EffectiveModelLimits
from eggpool.errors import ContextLimitExceededError
from eggpool.request.limits import (
    estimate_input_tokens,
    estimate_reservation_tokens,
    requested_output_tokens,
)


class MockCatalogCache:
    def __init__(self, model_info: dict | None) -> None:
        self._model_info = model_info

    def get_effective_limits(
        self, model_id: str, provider_id: str | None
    ) -> EffectiveModelLimits | None:
        if self._model_info is None:
            return None
        raw = self._model_info.get("effective_limits")
        if not isinstance(raw, dict) or not raw:
            return None
        return EffectiveModelLimits(
            context_tokens=raw.get("context_tokens"),
            input_tokens=raw.get("input_tokens"),
            output_tokens=raw.get("output_tokens"),
            enforce=raw.get("enforce", True),
            context_source=raw.get("context_source"),
            input_source=raw.get("input_source"),
            output_source=raw.get("output_source"),
        )


def _catalog_model(context_tokens: int) -> dict[str, object]:
    return {
        "model_id": "m1",
        "protocol": "openai",
        "effective_limits": {
            "context_tokens": context_tokens,
            "input_tokens": None,
            "output_tokens": None,
            "enforce": True,
        },
    }


def test_context_input_estimate_is_conservative_and_unbounded() -> None:
    assert estimate_input_tokens(b"") == 1_000
    assert estimate_input_tokens(b"x" * 6000) == 2_000
    assert estimate_input_tokens(b"x" * 6001) == 2_001
    assert estimate_input_tokens(b"x" * 1_000_000) == 333_334


def test_reservation_input_estimate_is_bounded() -> None:
    assert estimate_reservation_tokens(b"x" * 1_000_000) == 128_000


@pytest.mark.parametrize("value", [True, 1.5, "2000", 0, -1])
def test_requested_output_tokens_rejects_non_positive_integers(value: object) -> None:
    assert requested_output_tokens({"max_tokens": value}, "openai") is None


def test_requested_output_tokens_prefers_modern_openai_field() -> None:
    assert (
        requested_output_tokens(
            {"max_completion_tokens": 2_000, "max_tokens": 1_000},
            "openai",
        )
        == 2_000
    )


def test_requested_output_tokens_falls_back_to_legacy_field() -> None:
    assert (
        requested_output_tokens(
            {"max_completion_tokens": 0, "max_tokens": 1_000},
            "openai",
        )
        == 1_000
    )


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


def test_large_request_cannot_bypass_context_limit_estimate_cap() -> None:
    cache = MockCatalogCache(
        {
            "effective_limits": {
                "context_tokens": 200_000,
                "enforce": True,
            }
        }
    )
    with pytest.raises(ContextLimitExceededError):
        _check_context_limits(
            model_id="m1",
            provider_id=None,
            body=b"x" * 1_000_000,
            payload={"model": "m1"},
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


def test_unsuffixed_request_uses_conservative_provider_limits() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account("wide", "provider-wide", [_catalog_model(10_000)])
    cache.update_from_account("narrow", "provider-narrow", [_catalog_model(1_000)])

    with pytest.raises(ContextLimitExceededError) as exc_info:
        _check_context_limits(
            model_id="m1",
            provider_id=None,
            body=b"x" * 3_003,
            payload={"model": "m1"},
            protocol="openai",
            catalog_cache=cache,
        )

    assert exc_info.value.max_context_tokens == 1_000


def test_provider_suffix_keeps_provider_specific_limits() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account("wide", "provider-wide", [_catalog_model(10_000)])
    cache.update_from_account("narrow", "provider-narrow", [_catalog_model(1_000)])

    _check_context_limits(
        model_id="m1",
        provider_id="provider-wide",
        body=b"x" * 3_003,
        payload={"model": "m1"},
        protocol="openai",
        catalog_cache=cache,
    )


def test_provider_suffix_does_not_borrow_another_providers_limits() -> None:
    cache = ModelCatalogCache()
    cache.update_from_account("narrow", "provider-narrow", [_catalog_model(1_000)])

    _check_context_limits(
        model_id="m1",
        provider_id="provider-without-model",
        body=b"x" * 3_003,
        payload={"model": "m1"},
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
