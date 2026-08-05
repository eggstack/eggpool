"""Durability and generation-publication tests for model quarantine."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from eggpool.catalog.fetcher import FetchResult
from eggpool.catalog.service import AccountCatalogOutcome, CatalogService
from eggpool.db.repositories import ModelQuarantineRepository
from eggpool.errors import (
    ModelQuarantineHydrationError,
    ModelQuarantineRecoveryError,
)
from eggpool.failure import EvidenceProvenance, ModelQuarantine, QuarantineState
from eggpool.generation_factory import (
    _clear_model_reappearance_durable_first,
    _hydrate_model_quarantine,
)
from eggpool.models.config import AppConfig, ProviderConfig
from eggpool.runtime_manager import CandidateOwnershipState, RuntimeGenerationCandidate


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "provider_id": "provider-a",
        "account_id": "account-a",
        "canonical_model_id": "canonical-model",
        "upstream_model_id": "upstream-model",
        "upstream_protocol": "anthropic",
        "state": "quarantined",
        "evidence_provenance": "runtime_http",
        "reason": "model_unavailable",
        "first_observed_epoch": 100.0,
        "last_observed_epoch": 101.0,
        "observation_count": 2,
        "expiry_epoch": 4102444800.0,
        "cleared_at_epoch": None,
        "clear_reason": None,
        "last_status_code": 404,
        "last_error_class": "ModelUnavailableError",
    }
    row.update(overrides)
    return row


@pytest.mark.asyncio
async def test_zero_row_hydration_is_a_successful_empty_publication() -> None:
    repo = MagicMock()
    repo.list_all = AsyncMock(return_value=[])
    quarantine = ModelQuarantine()

    await _hydrate_model_quarantine(repo, quarantine)

    assert quarantine.list_entries() == []


@pytest.mark.asyncio
async def test_non_empty_hydration_preserves_the_durable_identity() -> None:
    repo = MagicMock()
    repo.list_all = AsyncMock(return_value=[_row()])
    quarantine = ModelQuarantine()

    await _hydrate_model_quarantine(repo, quarantine)

    entry = quarantine.list_entries()[0]
    assert entry.state is QuarantineState.QUARANTINED
    assert entry.provider_id == "provider-a"
    assert entry.account_id == "account-a"
    assert entry.canonical_model_id == "canonical-model"
    assert entry.upstream_model_id == "upstream-model"
    assert entry.upstream_protocol == "anthropic"
    assert entry.observation_count == 2


@pytest.mark.asyncio
async def test_hydration_read_failure_rejects_generation_state() -> None:
    repo = MagicMock()
    repo.list_all = AsyncMock(side_effect=RuntimeError("database unavailable"))

    with pytest.raises(ModelQuarantineHydrationError):
        await _hydrate_model_quarantine(repo, ModelQuarantine())


@pytest.mark.asyncio
async def test_hydration_row_conversion_failure_is_not_skipped() -> None:
    repo = MagicMock()
    repo.list_all = AsyncMock(return_value=[_row(state="future_state")])

    with pytest.raises(ModelQuarantineHydrationError):
        await _hydrate_model_quarantine(repo, ModelQuarantine())


@pytest.mark.asyncio
async def test_repository_rejects_malformed_hydration_timestamp() -> None:
    db = MagicMock()
    db.fetch_all = AsyncMock(return_value=[{"first_observed": "not-a-timestamp"}])

    with pytest.raises(ModelQuarantineHydrationError):
        await ModelQuarantineRepository(db).list_all()


@pytest.mark.asyncio
async def test_candidate_abort_closes_resources_after_hydration_failure() -> None:
    repo = MagicMock()
    repo.list_all = AsyncMock(side_effect=RuntimeError("database unavailable"))
    close = AsyncMock()
    candidate = RuntimeGenerationCandidate(generation_id=7)
    candidate.register_resource("candidate-client", close)

    with pytest.raises(ModelQuarantineHydrationError):
        await _hydrate_model_quarantine(repo, ModelQuarantine())
    await candidate.abort(cause=ModelQuarantineHydrationError("hydration failed"))

    close.assert_awaited_once()
    assert candidate.ownership_state is CandidateOwnershipState.ABORTED


class _DurableQuarantineRepo:
    def __init__(self, result: int = 1) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    async def mark_cleared(self, **kwargs: object) -> int:
        self.calls.append(kwargs)
        return self.result


class _QuarantineApplier:
    def __init__(self, quarantine: ModelQuarantine) -> None:
        self.quarantine = quarantine
        self.calls: list[dict[str, object]] = []

    def clear_authoritative_reappearance(self, **kwargs: object) -> bool:
        self.calls.append(kwargs)
        return self.quarantine.clear_authoritative_reappearance(**kwargs)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_reappearance_clear_is_durable_first_and_exactly_scoped() -> None:
    quarantine = ModelQuarantine()
    quarantine.record_observation(
        provider_id="provider-a",
        account_id="account-a",
        canonical_model_id="canonical-model",
        upstream_model_id="upstream-model",
        upstream_protocol="anthropic",
        evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
        reason="model_unavailable",
        now=100.0,
    )
    order: list[str] = []
    durable = _DurableQuarantineRepo()
    applier = _QuarantineApplier(quarantine)
    backoff = MagicMock()
    backoff.clear_success = AsyncMock(side_effect=lambda **_: order.append("backoff"))
    account_repo = MagicMock()
    account_repo.get_id_by_name = AsyncMock(return_value=42)

    original_mark = durable.mark_cleared

    async def mark_cleared(**kwargs: object) -> int:
        order.append("durable")
        return await original_mark(**kwargs)

    durable.mark_cleared = mark_cleared  # type: ignore[method-assign]
    original_clear = applier.clear_authoritative_reappearance

    def clear_memory(**kwargs: object) -> bool:
        order.append("memory")
        return original_clear(**kwargs)

    applier.clear_authoritative_reappearance = clear_memory  # type: ignore[method-assign]

    await _clear_model_reappearance_durable_first(
        account_name="account-a",
        provider_id="provider-a",
        models=[
            {
                "canonical_model_id": "canonical-model",
                "upstream_model_id": "upstream-model",
                "model_id": "display-alias",
                "protocol": "anthropic",
            }
        ],
        model_quarantine_repo=durable,
        effects_applier=applier,
        account_backoff_repo=backoff,
        account_repo=account_repo,
    )

    assert order == ["durable", "memory", "backoff"]
    assert durable.calls[0]["canonical_model_id"] == "canonical-model"
    assert durable.calls[0]["upstream_model_id"] == "upstream-model"
    assert durable.calls[0]["upstream_protocol"] == "anthropic"
    assert not quarantine.is_model_quarantined(
        "provider-a",
        "account-a",
        "canonical-model",
        "upstream-model",
        "anthropic",
    )


@pytest.mark.asyncio
async def test_already_cleared_durable_state_allows_idempotent_memory_clear() -> None:
    quarantine = ModelQuarantine()
    quarantine.record_observation(
        provider_id="provider-a",
        account_id="account-a",
        canonical_model_id="canonical-model",
        upstream_model_id="upstream-model",
        upstream_protocol="openai",
        evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
        reason="model_unavailable",
    )
    durable = _DurableQuarantineRepo(result=0)
    applier = _QuarantineApplier(quarantine)

    await _clear_model_reappearance_durable_first(
        account_name="account-a",
        provider_id="provider-a",
        models=[{"model_id": "canonical-model", "protocol": "openai"}],
        model_quarantine_repo=durable,
        effects_applier=applier,
        account_backoff_repo=MagicMock(clear_success=AsyncMock()),
        account_repo=MagicMock(get_id_by_name=AsyncMock(return_value=None)),
    )

    assert len(applier.calls) == 1
    assert not quarantine.is_model_quarantined(
        "provider-a", "account-a", "canonical-model", "canonical-model", "openai"
    )


@pytest.mark.asyncio
async def test_durable_clear_failure_preserves_memory_and_backoff() -> None:
    quarantine = ModelQuarantine()
    quarantine.record_observation(
        provider_id="provider-a",
        account_id="account-a",
        canonical_model_id="canonical-model",
        upstream_model_id="canonical-model",
        upstream_protocol="openai",
        evidence_provenance=EvidenceProvenance.RUNTIME_HTTP,
        reason="model_unavailable",
    )
    durable = MagicMock()
    durable.mark_cleared = AsyncMock(side_effect=RuntimeError("locked"))
    applier = _QuarantineApplier(quarantine)
    backoff = MagicMock()
    backoff.clear_success = AsyncMock()

    with pytest.raises(ModelQuarantineRecoveryError):
        await _clear_model_reappearance_durable_first(
            account_name="account-a",
            provider_id="provider-a",
            models=[{"model_id": "canonical-model", "protocol": "openai"}],
            model_quarantine_repo=durable,
            effects_applier=applier,
            account_backoff_repo=backoff,
            account_repo=MagicMock(get_id_by_name=AsyncMock(return_value=42)),
        )

    assert applier.calls == []
    backoff.clear_success.assert_not_awaited()
    assert quarantine.is_model_quarantined(
        "provider-a", "account-a", "canonical-model", "canonical-model", "openai"
    )


@pytest.mark.asyncio
async def test_catalog_does_not_publish_cache_when_reappearance_recovery_fails() -> (
    None
):
    config = AppConfig(
        providers={
            "provider-a": ProviderConfig(
                id="provider-a",
                base_url="https://provider.example",
                protocols=["openai"],
            )
        },
        models={"catalog_withdrawal_policy": "confirmed_once"},
    )
    service = CatalogService(
        config=config,
        registry=MagicMock(),
        db=MagicMock(),
        client_pool=AsyncMock(spec=httpx.AsyncClient),
    )
    service.set_model_reappearance_callback(
        AsyncMock(side_effect=ModelQuarantineRecoveryError("durable clear failed"))
    )

    with patch(
        "eggpool.catalog.service.fetch_models_for_account",
        new=AsyncMock(
            return_value=FetchResult(
                response={"data": [{"id": "new-model", "protocol": "openai"}]},
                latency_ms=1,
                status_code=200,
                error=None,
                model_count=1,
            )
        ),
    ):
        outcome, update = await service._fetch_and_process_account(
            "account-a", "key", "provider-a", AsyncMock(spec=httpx.AsyncClient)
        )

    assert outcome is AccountCatalogOutcome.FAILED
    assert update is None
    assert service.cache.get_supporting_accounts("new-model") == set()
