"""Regression tests for the catalog non-destructive refresh contract.

The catalog refresh must preserve prior support on failed, empty, and
partial refreshes. Only an explicitly authorized destructive update
(``authoritative=True, allow_withdrawals=True``) may remove account
support from the cache.  These tests cover the cache-level guard, the
service-level outcome classification, the per-cycle refresh summary,
and the operator-facing gate diagnostics on
``Router.explain_account_eligibility``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from eggpool.catalog.cache import (
    AccountCatalogOutcome,
    AccountCatalogUpdateResult,
    ModelCatalogCache,
)
from eggpool.catalog.service import CatalogService
from eggpool.models.config import AppConfig, ProviderConfig
from eggpool.routing.router import Router

# ===================================================================
# Cache-level guard (Phase 1+2)
# ===================================================================


def test_update_from_account_default_is_non_destructive() -> None:
    """Calling ``update_from_account`` without flags must preserve prior
    support; the destructive path requires the operator to opt in.
    """
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1", "opencode-go", [{"model_id": "gpt-4", "protocol": "openai"}]
    )
    cache.update_from_account(
        "acct2", "opencode-go", [{"model_id": "gpt-4", "protocol": "openai"}]
    )

    # Default flags: non-destructive.
    result = cache.update_from_account("acct1", "opencode-go", [])
    assert isinstance(result, AccountCatalogUpdateResult)
    assert result.added_support == 0
    assert result.withdrawn_support == 0
    assert result.preserved_support == 1
    assert cache.get_supporting_accounts("gpt-4") == {"acct1", "acct2"}


def test_update_from_account_authoritative_alone_is_non_destructive() -> None:
    """``authoritative=True`` without ``allow_withdrawals`` must not
    withdraw. Operators who want to record an authoritative-only update
    can do so without losing prior support.
    """
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1", "opencode-go", [{"model_id": "gpt-4", "protocol": "openai"}]
    )

    result = cache.update_from_account(
        "acct1",
        "opencode-go",
        [],
        authoritative=True,
        allow_withdrawals=False,
    )
    assert result.added_support == 0
    assert result.withdrawn_support == 0
    assert result.preserved_support == 1
    assert cache.get_supporting_accounts("gpt-4") == {"acct1"}


def test_update_from_account_destructive_path_requires_both_flags() -> None:
    """Destructive withdrawal requires both ``authoritative`` AND
    ``allow_withdrawals``. The ``result.withdrawn_support`` counter
    reports the actual row count removed, not just whether withdrawal
    was requested.
    """
    cache = ModelCatalogCache()
    cache.update_from_account(
        "acct1", "opencode-go", [{"model_id": "gpt-4", "protocol": "openai"}]
    )
    cache.update_from_account(
        "acct2", "opencode-go", [{"model_id": "claude-3", "protocol": "anthropic"}]
    )

    result = cache.update_from_account(
        "acct1",
        "opencode-go",
        [{"model_id": "new-model", "protocol": "openai"}],
        authoritative=True,
        allow_withdrawals=True,
    )
    assert result.added_support == 1
    assert result.withdrawn_support == 1  # gpt-4 was the only row for acct1
    assert cache.get_supporting_accounts("gpt-4") == set()
    assert cache.get_supporting_accounts("claude-3") == {"acct2"}
    assert cache.get_supporting_accounts("new-model") == {"acct1"}


# ===================================================================
# Service-level outcome classification (Phase 5)
# ===================================================================


def _make_config(policy: str | None = None) -> AppConfig:
    return AppConfig(
        providers={
            "opencode-go": ProviderConfig(
                id="opencode-go",
                base_url="https://opencode.example",
                protocols=["openai"],
            )
        },
        **({"models": {"catalog_withdrawal_policy": policy}} if policy else {}),
    )


@dataclass
class _State:
    name: str
    enabled: bool = True


def _make_registry(names: list[str]) -> MagicMock:
    registry = MagicMock()
    states = [_State(name=n) for n in names]
    registry.get_enabled_states.return_value = states
    registry.get_api_key.return_value = "test-key"
    registry.get_provider_for_account.side_effect = lambda n: "opencode-go"
    return registry


def _make_service(
    config: AppConfig,
    *,
    registry_names: list[str] | None = None,
    ping_repo: AsyncMock | None = None,
    client: AsyncMock | None = None,
) -> CatalogService:
    return CatalogService(
        config=config,
        registry=_make_registry(registry_names or ["acct1"]),
        db=MagicMock(),
        client_pool=client or AsyncMock(spec=httpx.AsyncClient),
        ping_repo=ping_repo,
    )


def _ok_response(payload: dict[str, Any] | list[Any]) -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = payload
    response.raise_for_status = MagicMock()
    return response


@pytest.mark.asyncio
async def test_failed_refresh_does_not_touch_cache(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A network/5xx/auth failure during refresh must leave the cache
    entirely intact. Prior support survives even when the provider is
    unreachable.
    """
    config = _make_config()
    service = _make_service(config)
    service.cache.update_from_account(
        "acct1", "opencode-go", [{"model_id": "gpt-4", "protocol": "openai"}]
    )
    support_before = set(service.cache.get_supporting_accounts("gpt-4"))

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("Connection refused"))

    with caplog.at_level(logging.WARNING, logger="eggpool.catalog.service"):
        outcome, update = await service._fetch_and_process_account(
            "acct1", "test-key", "opencode-go", mock_client
        )

    assert outcome is AccountCatalogOutcome.FAILED
    assert update is None
    assert service.cache.get_supporting_accounts("gpt-4") == support_before
    assert any("preserved prior support" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_empty_response_preserves_prior_support() -> None:
    """An HTTP 200 with an empty ``data`` list must be reported as
    ``SUCCESS_EMPTY`` and must preserve prior support. The destructive
    path is not taken regardless of the configured policy.
    """
    config = _make_config()
    service = _make_service(config)
    service.cache.update_from_account(
        "acct1", "opencode-go", [{"model_id": "gpt-4", "protocol": "openai"}]
    )

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get = AsyncMock(return_value=_ok_response({"data": []}))

    outcome, update = await service._fetch_and_process_account(
        "acct1", "test-key", "opencode-go", mock_client
    )

    assert outcome is AccountCatalogOutcome.SUCCESS_EMPTY
    assert update is not None
    assert update.preserved_support == 1
    assert update.withdrawn_support == 0
    assert service.cache.get_supporting_accounts("gpt-4") == {"acct1"}


@pytest.mark.asyncio
async def test_partial_response_disables_withdrawals() -> None:
    """A successful response that fails protocol resolution on at least
    one model is classified ``SUCCESS_PARTIAL`` and the destructive
    flag is forced off for that cycle, even when the operator opted
    into a destructive policy.
    """
    config = _make_config(policy="confirmed_once")
    service = _make_service(config)
    service.cache.update_from_account(
        "acct1",
        "opencode-go",
        [{"model_id": "gpt-4", "protocol": "openai"}],
    )

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get = AsyncMock(
        return_value=_ok_response({"data": [{"id": "unknown-id"}]})
    )

    outcome, update = await service._fetch_and_process_account(
        "acct1", "test-key", "opencode-go", mock_client
    )

    assert outcome is AccountCatalogOutcome.SUCCESS_PARTIAL
    assert update is not None
    assert update.withdrawn_support == 0
    assert update.preserved_support == 1


@pytest.mark.asyncio
async def test_default_policy_is_non_destructive_on_authoritative() -> None:
    """A fully-resolved response under the default
    ``preserve_until_health`` policy must add new support and preserve
    any support not returned by the upstream.
    """
    config = _make_config()
    service = _make_service(config)
    service.cache.update_from_account(
        "acct1",
        "opencode-go",
        [{"model_id": "gpt-4", "protocol": "openai"}],
    )

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.get = AsyncMock(
        return_value=_ok_response({"data": [{"id": "gpt-4"}, {"id": "gpt-4o-mini"}]})
    )

    outcome, update = await service._fetch_and_process_account(
        "acct1", "test-key", "opencode-go", mock_client
    )

    assert outcome is AccountCatalogOutcome.SUCCESS_AUTHORITATIVE
    assert update is not None
    assert update.withdrawn_support == 0
    assert update.added_support == 1
    assert update.preserved_support == 1
    assert service.cache.get_supporting_accounts("gpt-4") == {"acct1"}
    assert service.cache.get_supporting_accounts("gpt-4o-mini") == {"acct1"}


# ===================================================================
# Per-cycle operational event logging (Phase 9)
# ===================================================================


@pytest.mark.asyncio
async def test_refresh_summary_logs_outcome_counts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The refresh-summary log must enumerate every outcome category
    on a single line so operators can scan the log for catalog
    uncertainty without enabling debug logging.
    """
    config = _make_config()
    service = _make_service(config)

    # Three outcomes in one cycle: SUCCESS_AUTHORITATIVE, FAILED,
    # SUCCESS_EMPTY.
    with caplog.at_level(logging.INFO, logger="eggpool.catalog.service"):
        service._log_refresh_summary(
            outcomes=[
                AccountCatalogOutcome.SUCCESS_AUTHORITATIVE,
                AccountCatalogOutcome.FAILED,
                AccountCatalogOutcome.SUCCESS_EMPTY,
            ],
            total_accounts=3,
        )

    messages = [rec.message for rec in caplog.records]
    assert any("Catalog refresh summary" in m for m in messages)
    summary = next(m for m in messages if "Catalog refresh summary" in m)
    assert "policy=preserve_until_health" in summary
    assert "authoritative=1" in summary
    assert "empty=1" in summary
    assert "failed=1" in summary


# ===================================================================
# Phase 7: gate diagnostics on explain_account_eligibility
# ===================================================================


def _make_router_for_explain() -> Router:
    router = Router.__new__(Router)
    router._registry = MagicMock()
    router._registry._states = {}
    router._quota_estimator = MagicMock()
    router._health_manager = MagicMock()
    router._providers_config = MagicMock()
    router._providers_config.providers = {}
    router._catalog_service = MagicMock()
    router._catalog = MagicMock()
    router._stale_after_s = 0.0
    router._local_quota_mode = "score_only"
    return router


@pytest.mark.asyncio
async def test_explain_account_eligibility_default_omits_gates() -> None:
    """The default ``explain_account_eligibility`` response does not
    include the gate breakdown; ``include_gates=True`` is opt-in.
    """
    router = _make_router_for_explain()
    rows = await router.explain_account_eligibility(model_id="gpt-4")
    assert rows == []
    assert all("gates" not in row for row in rows)


def test_collect_gate_status_returns_expected_keys() -> None:
    """``_collect_gate_status`` returns a structured dict with the
    documented keys, including the final ``final_eligible`` slot that
    ``explain_account_eligibility`` fills in.
    """
    from eggpool.accounts.state import AccountRuntimeState

    router = _make_router_for_explain()
    state = AccountRuntimeState(
        name="acct1",
        enabled=True,
        health_state="healthy",
    )
    router._registry.get_provider_for_account.return_value = "opencode-go"
    router._registry.has_usable_credentials.return_value = True
    router._registry.get_provider_protocols.return_value = ["openai"]

    gates = router._collect_gate_status(
        state=state,
        model_id="gpt-4",
        provider_id="opencode-go",
        protocol="openai",
        transcode_eligibility=None,
    )
    expected_keys = {
        "config_enabled",
        "credentials_usable",
        "health_state",
        "circuit_closed",
        "provider_id_registry",
        "provider_id_catalog",
        "provider_match",
        "provider_supports_protocol",
        "model_support_row",
        "model_support_enabled",
        "fresh_support",
        "provider_model_metadata_exists",
        "provider_model_protocol",
        "protocol_match",
        "local_quota_gate",
        "final_eligible",
    }
    assert expected_keys.issubset(gates.keys())


# ===================================================================
# Cross-account protocol-poisoning regression
# ===================================================================


def test_partial_refresh_does_not_clobber_shared_provider_protocol() -> None:
    """A single account's partial refresh must not poison the shared
    ``_provider_models[(model_id, provider_id)]`` row for its siblings.

    ``_provider_models`` is keyed by ``(model_id, provider_id)`` and
    shared by every account that lists that provider. When one
    ``opencode-go`` account delivers a model with ``protocol=None``
    (transient upstream parse error, unresolved family prefix, or a
    normalized list whose protocol could not be re-derived this
    cycle), the cache must keep the previously-resolved protocol so
    the other ``opencode-go-XXXX`` siblings remain routable.
    """
    cache = ModelCatalogCache()

    for account in ("opencode-go-0001", "opencode-go-0002", "opencode-go-0003"):
        cache.update_from_account(
            account,
            "opencode-go",
            [
                {
                    "model_id": "minimax-2.7",
                    "protocol": "openai",
                    "protocol_source": "upstream_metadata",
                }
            ],
        )

    for account in ("opencode-go-0001", "opencode-go-0002", "opencode-go-0003"):
        assert cache.is_account_model_available(
            account, "minimax-2.7", protocol="openai"
        ), f"{account} must be available before any partial refresh"

    cache.update_from_account(
        "opencode-go-0002",
        "opencode-go",
        [
            {
                "model_id": "minimax-2.7",
                "protocol": None,
                "protocol_source": None,
            }
        ],
        authoritative=True,
    )

    for account in ("opencode-go-0001", "opencode-go-0002", "opencode-go-0003"):
        assert cache.is_account_model_available(
            account, "minimax-2.7", protocol="openai"
        ), (
            f"{account} must remain available after a sibling partial "
            "refresh; the prior resolved protocol must not be clobbered"
        )

    entry = cache.get_provider_model_entry("minimax-2.7", "opencode-go")
    assert entry is not None
    assert entry.get("protocol") == "openai"


def test_partial_refresh_does_not_clobber_shared_provider_protocol_multiple() -> None:
    """Repeated partial refreshes across multiple sibling accounts must
    not cumulatively downgrade the shared per-provider protocol row.
    """
    cache = ModelCatalogCache()

    for account in ("opencode-go-0001", "opencode-go-0002", "opencode-go-0003"):
        cache.update_from_account(
            account,
            "opencode-go",
            [
                {
                    "model_id": "minimax-2.7",
                    "protocol": "openai",
                    "protocol_source": "upstream_metadata",
                }
            ],
        )

    for partial_account in ("opencode-go-0002", "opencode-go-0003", "opencode-go-0002"):
        cache.update_from_account(
            partial_account,
            "opencode-go",
            [
                {
                    "model_id": "minimax-2.7",
                    "protocol": None,
                    "protocol_source": None,
                }
            ],
            authoritative=True,
        )

    for account in ("opencode-go-0001", "opencode-go-0002", "opencode-go-0003"):
        assert cache.is_account_model_available(
            account, "minimax-2.7", protocol="openai"
        ), f"{account} must stay routable across repeated partial refreshes"


def test_explicit_destructive_update_can_still_clear_protocol() -> None:
    """The sibling-wins guard must not block operator-initiated
    destructive withdrawals. With ``authoritative=True and
    allow_withdrawals=True`` the cache must honor the new state even
    if it carries ``protocol=None`` so the operator can intentionally
    clear a previously-resolved protocol.
    """
    cache = ModelCatalogCache()

    cache.update_from_account(
        "opencode-go-0001",
        "opencode-go",
        [
            {
                "model_id": "minimax-2.7",
                "protocol": "openai",
                "protocol_source": "upstream_metadata",
            }
        ],
    )

    cache.update_from_account(
        "opencode-go-0001",
        "opencode-go",
        [
            {
                "model_id": "minimax-2.7",
                "protocol": None,
                "protocol_source": None,
            }
        ],
        authoritative=True,
        allow_withdrawals=True,
    )

    assert not cache.is_account_model_available(
        "opencode-go-0001", "minimax-2.7", protocol="openai"
    ), (
        "Explicit destructive update with both flags must be able to "
        "clear the resolved protocol and withdraw the account support"
    )
