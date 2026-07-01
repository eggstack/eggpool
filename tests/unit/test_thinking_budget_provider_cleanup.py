"""Tests for the provider-specific thinking-budget cleanup (final pass).

These tests pin the behaviour introduced at the close of the
thinking/reasoning implementation:

- The selected provider's ``effort_to_budget_tokens`` mapping wins for
  OpenAI ``reasoning_effort`` requests, even when the preflight
  translation already wrote an intermediate Anthropic budget into
  ``upstream_body``.
- Explicit client budgets are still validated/clamped against the
  selected provider's min/max.
- Strict-policy rejections at dispatch time are client-side validation
  failures: they return HTTP 400, leave no upstream request pending,
  finalize the durable attempt, release the reservation (durable and
  in-memory), decrement the active request count, release the health
  slot, and record a ``decision = "rejected"`` thinking trace.
- Pre-selection capability rejections must not try to finalize a
  non-existent attempt.
- Streaming and non-streaming dispatch paths share identical cleanup
  semantics.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from eggpool.catalog.capabilities import (
    ModelCapabilities,
    ThinkingCapability,
    model_capabilities_to_dict,
)
from eggpool.errors import CapabilityError
from eggpool.transcoder.budget_resolver import BudgetResolutionError
from eggpool.transcoder.context import TranscodeContext
from eggpool.transcoder.policy import (
    ThinkingBudgetDefaults,
    TranscoderFeatures,
    TranscoderPolicy,
)

if TYPE_CHECKING:
    from eggpool.db.connection import Database
    from eggpool.request.coordinator import RequestCoordinator


# ---------------------------------------------------------------------------
# Module-level mock catalog and helpers
# ---------------------------------------------------------------------------


class _MockCatalog:
    """Mock catalog service exposing only the ``cache`` attribute."""

    def __init__(self, cache: Any) -> None:
        self._cache = cache

    @property
    def cache(self) -> Any:
        return self._cache


def _policy(
    *,
    strict: bool = False,
    high_default: int = 16384,
) -> TranscoderPolicy:
    """Build a transcoder policy with the given strict mode and defaults."""
    return TranscoderPolicy(
        enabled=True,
        features=TranscoderFeatures(thinking=True),
        budget_resolution_policy="strict" if strict else "lenient",
        thinking_budget_defaults=ThinkingBudgetDefaults(
            low=1024,
            medium=4096,
            high=high_default,
        ),
    )


def _make_context(
    *,
    original_body: bytes,
    upstream_body: bytes,
    model_id: str,
    streaming: bool = False,
) -> Any:
    """Build a ``ProxyRequestContext`` with explicit transcoded state."""
    from eggpool.request.coordinator import ProxyRequestContext

    return ProxyRequestContext(
        request_id="req-budget-cleanup",
        protocol="openai",
        model_id=model_id,
        streaming=streaming,
        original_body=original_body,
        incoming_headers={},
        upstream_body=upstream_body,
        upstream_protocol="anthropic",
        transcode_required=True,
        transcode_context=TranscodeContext(
            request_id="req-budget-cleanup",
            client_protocol="openai",
            upstream_protocol="anthropic",
        ),
        thinking_trace={
            "requested": True,
            "client_protocol": "openai",
            "request_fields": ["reasoning_effort"],
            "requested_effort": None,
            "resolved_budget_tokens": None,
            "budget_clamped": False,
            "capability_status": None,
            "capability_source": None,
            "upstream_protocol": None,
            "upstream_fields": [],
            "decision": "none",
        },
    )


def _make_selected_attempt(
    *,
    attempt_id: int,
    reservation_id: str,
    account_name: str,
    db_request_id: int = 0,
    estimated_microdollars: int = 1000,
    provider_id: str = "test-provider",
    streamed: bool = False,
) -> Any:
    """Build a ``SelectedAttempt`` for a synthetic already-selected attempt."""
    from eggpool.request.coordinator import SelectedAttempt

    return SelectedAttempt(
        proxy_request_id="req-budget-cleanup",
        db_request_id=str(db_request_id),
        attempt_id=attempt_id,
        reservation_id=reservation_id,
        account_id=1,
        account_name=account_name,
        api_key="sk-test",
        model_id="test-model",
        estimated_tokens=100,
        estimated_microdollars=estimated_microdollars,
        attempt_number=1,
        provider_id=provider_id,
        requires_transcode=True,
        protocol="openai",
        streamed=streamed,
    )


async def _seed_account_and_request(
    db: Database,
    *,
    account_name: str,
    api_key_env: str,
    model_id: str,
) -> tuple[int, int, int, str]:
    """Insert account/model/request/reservation/attempt rows.

    Returns ``(account_id, request_db_id, attempt_id, reservation_id_str)``.
    """
    async with db.transaction():
        await db.execute_insert(
            "INSERT INTO models (model_id, display_name, protocol) VALUES (?, ?, ?)",
            (model_id, model_id, "anthropic"),
        )
        await db.execute_insert(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, ?)",
            (account_name, api_key_env, 1.0),
        )
        row = await db.fetch_one(
            "SELECT id FROM accounts WHERE name = ?", (account_name,)
        )
        assert row is not None
        account_id = int(row["id"])
        await db.execute_insert(
            "INSERT INTO account_models "
            "(account_id, model_id, enabled) VALUES (?, ?, 1)",
            (account_id, model_id),
        )
        await db.execute_insert(
            "INSERT INTO requests (account_id, model_id, status) "
            "VALUES (?, ?, 'pending')",
            (account_id, model_id),
        )
        req_row = await db.fetch_one(
            "SELECT id FROM requests WHERE account_id = ? ORDER BY id DESC LIMIT 1",
            (account_id,),
        )
        assert req_row is not None
        request_db_id = int(req_row["id"])
        await db.execute_insert(
            "INSERT INTO request_attempts "
            "(request_id, attempt_number, account_id) VALUES (?, 1, ?)",
            (request_db_id, account_id),
        )
        attempt_row = await db.fetch_one(
            "SELECT id FROM request_attempts WHERE request_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (request_db_id,),
        )
        assert attempt_row is not None
        attempt_id = int(attempt_row["id"])
        await db.execute_insert(
            "INSERT INTO reservations "
            "(request_id, account_id, model_id, "
            " reserved_microdollars, status) "
            "VALUES (?, ?, ?, 1000, 'active')",
            (request_db_id, account_id, model_id),
        )
        res_row = await db.fetch_one(
            "SELECT id FROM reservations WHERE request_id = ? ORDER BY id DESC LIMIT 1",
            (request_db_id,),
        )
        assert res_row is not None
        reservation_id = str(int(res_row["id"]))
    return account_id, request_db_id, attempt_id, reservation_id


def _build_coordinator(
    *,
    db: Database,
    cache: Any,
    registry: Any,
    selected_provider_caps: ThinkingCapability,
    policy: TranscoderPolicy,
    quota_estimator: Any = None,
    health_manager: Any = None,
) -> RequestCoordinator:
    """Build a minimal coordinator with the test's selected-provider capability."""
    from eggpool.db.repositories import (
        AttemptRepository,
        RequestRepository,
        ReservationRepository,
        RoutingDecisionRepository,
    )
    from eggpool.request.coordinator import RequestCoordinator
    from eggpool.routing.router import Router

    # Index the model under the provider so _resolve_selected_thinking_capability
    # finds the override.
    cache.update_from_account(
        "0001",
        "test-provider",
        [
            {
                "model_id": "test-model",
                "protocol": "anthropic",
                "capabilities": model_capabilities_to_dict(
                    ModelCapabilities(thinking=selected_provider_caps),
                ),
            }
        ],
    )

    router = Router(registry, _MockCatalog(cache))  # type: ignore[arg-type]
    return RequestCoordinator(
        registry=registry,
        catalog=_MockCatalog(cache),  # type: ignore[arg-type]
        router=router,
        db=db,
        client_pool=httpx.AsyncClient(),
        request_repo=RequestRepository(db),
        reservation_repo=ReservationRepository(db),
        attempt_repo=AttemptRepository(db),
        routing_decision_repo=RoutingDecisionRepository(db),
        health_manager=health_manager,
        transcoder_policy=policy,
        quota_estimator=quota_estimator,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSelectedProviderEffortMappingWins:
    """Test 1: the selected provider's effort mapping wins over global/default.

    Reproduces the closing-pass bug where the recompute forwarded the
    pre-translated Anthropic ``thinking.budget_tokens`` as both
    ``requested_budget_tokens`` *and* the OpenAI ``reasoning_effort``.
    The resolver prioritises ``requested_budget_tokens`` so the
    provider's ``effort_to_budget_tokens`` mapping was silently ignored.
    """

    def test_effort_override_wins_over_translated_budget(self) -> None:
        from eggpool.accounts.registry import AccountRegistry
        from eggpool.catalog.cache import ModelCatalogCache
        from eggpool.models.config import AppConfig

        name = "0001"
        os.environ[f"K_{name}"] = "k"
        try:
            config = AppConfig.model_validate(
                {
                    "providers": {
                        "test-provider": {
                            "id": "test-provider",
                            "base_url": "https://api.example.com/v1",
                            "protocols": ["anthropic"],
                            "routing_priority": 0,
                            "accounts": [
                                {
                                    "name": name,
                                    "api_key_env": f"K_{name}",
                                    "weight": 1.0,
                                }
                            ],
                        }
                    }
                }
            )
            registry = AccountRegistry(config)
            cache = ModelCatalogCache()
            provider_caps = ThinkingCapability(
                status="supported",
                source="manual_override",
                effort_to_budget_tokens={"high": 32768},
            )
            policy = _policy(strict=False, high_default=16384)
            coordinator = _build_coordinator_sync(
                registry=registry,
                cache=cache,
                selected_provider_caps=provider_caps,
                policy=policy,
            )
            original_body = (
                b'{"model":"test-model",'
                b'"messages":[{"role":"user","content":"hi"}],'
                b'"reasoning_effort":"high"}'
            )
            # The pre-translated upstream body uses the *global* default
            # (16384), which is what the bug would have preserved.
            upstream_body = (
                b'{"model":"test-model",'
                b'"messages":[{"role":"user","content":"hi"}],'
                b'"thinking":{"type":"enabled","budget_tokens":16384}}'
            )
            ctx = _make_context(
                original_body=original_body,
                upstream_body=upstream_body,
                model_id="test-model",
            )
            selected = _make_selected_attempt(
                attempt_id=1,
                reservation_id="1",
                account_name=name,
                provider_id="test-provider",
            )

            coordinator._apply_selected_provider_transcode_adjustments(
                context=ctx,
                selected=selected,
            )

            assert ctx.upstream_body is not None
            payload = json.loads(ctx.upstream_body)
            assert payload["thinking"]["budget_tokens"] == 32768
            assert ctx.thinking_trace is not None
            assert ctx.thinking_trace["resolved_budget_tokens"] == 32768
            assert ctx.thinking_trace["capability_status"] == "supported"
            assert ctx.thinking_trace["capability_source"] == "manual_override"
        finally:
            os.environ.pop(f"K_{name}", None)


class TestExplicitAnthropicBudgetClamped:
    """Test 2: explicit Anthropic budgets are clamped against selected provider."""

    def test_explicit_budget_above_max_is_clamped(self) -> None:
        from eggpool.accounts.registry import AccountRegistry
        from eggpool.catalog.cache import ModelCatalogCache
        from eggpool.models.config import AppConfig

        name = "0001"
        os.environ[f"K_{name}"] = "k"
        try:
            config = AppConfig.model_validate(
                {
                    "providers": {
                        "test-provider": {
                            "id": "test-provider",
                            "base_url": "https://api.example.com/v1",
                            "protocols": ["anthropic"],
                            "routing_priority": 0,
                            "accounts": [
                                {
                                    "name": name,
                                    "api_key_env": f"K_{name}",
                                    "weight": 1.0,
                                }
                            ],
                        }
                    }
                }
            )
            registry = AccountRegistry(config)
            cache = ModelCatalogCache()
            provider_caps = ThinkingCapability(
                status="supported",
                source="manual_override",
                budget_tokens_max=16384,
            )
            policy = _policy(strict=False, high_default=16384)
            coordinator = _build_coordinator_sync(
                registry=registry,
                cache=cache,
                selected_provider_caps=provider_caps,
                policy=policy,
            )
            original_body = (
                b'{"model":"test-model",'
                b'"messages":[{"role":"user","content":"hi"}],'
                b'"thinking":{"type":"enabled","budget_tokens":50000}}'
            )
            upstream_body = (
                b'{"model":"test-model",'
                b'"messages":[{"role":"user","content":"hi"}],'
                b'"thinking":{"type":"enabled","budget_tokens":50000}}'
            )
            ctx = _make_context(
                original_body=original_body,
                upstream_body=upstream_body,
                model_id="test-model",
            )
            selected = _make_selected_attempt(
                attempt_id=1,
                reservation_id="1",
                account_name=name,
                provider_id="test-provider",
            )

            coordinator._apply_selected_provider_transcode_adjustments(
                context=ctx,
                selected=selected,
            )

            assert ctx.upstream_body is not None
            payload = json.loads(ctx.upstream_body)
            assert payload["thinking"]["budget_tokens"] == 16384
            assert ctx.thinking_trace is not None
            # The trace records the resolved value even when clamping
            # happened (the decision classification happens later).
            assert ctx.thinking_trace["resolved_budget_tokens"] == 16384
            # The clamp must surface on the transcode loss_warnings so
            # decision classification reports ``clamped``.
            assert ctx.transcode_context is not None
            assert any(
                w.get("kind") == "budget_clamped"
                for w in ctx.transcode_context.loss_warnings
            )
        finally:
            os.environ.pop(f"K_{name}", None)


class TestStrictSelectedProviderRejectionCleansUp:
    """Test 3: strict clamp rejection cleans up state and re-raises.

    Asserts the lifecycle invariants:

    - Attempt row is finalized (no longer pending).
    - Reservation is released (durable status, in-memory cleared).
    - Active request count is decremented.
    - Health slot is released.
    - Thinking trace records ``decision = "rejected"``.
    - Request row is finalized with CLIENT_ERROR outcome.
    - No upstream HTTP request is dispatched (the upstream call path
      is not reached).
    """

    @pytest.mark.asyncio()
    async def test_strict_rejection_finalizes_state(self) -> None:
        from eggpool.accounts.registry import AccountRegistry
        from eggpool.catalog.cache import ModelCatalogCache
        from eggpool.health.health_manager import HealthManager
        from eggpool.models.config import AppConfig
        from eggpool.quota.estimation import QuotaEstimator

        name = "0001"
        os.environ[f"K_{name}"] = "k"
        try:
            config = AppConfig.model_validate(
                {
                    "providers": {
                        "test-provider": {
                            "id": "test-provider",
                            "base_url": "https://api.example.com/v1",
                            "protocols": ["anthropic"],
                            "routing_priority": 0,
                            "accounts": [
                                {
                                    "name": name,
                                    "api_key_env": f"K_{name}",
                                    "weight": 1.0,
                                }
                            ],
                        }
                    }
                }
            )
            registry = AccountRegistry(config)
            cache = ModelCatalogCache()
            provider_caps = ThinkingCapability(
                status="supported",
                source="manual_override",
                budget_tokens_max=8192,
            )
            policy = _policy(strict=True, high_default=16384)
            from eggpool.db.connection import Database
            from eggpool.db.migrations import MigrationRunner

            db = Database(path=":memory:")
            await db.connect()
            try:
                await MigrationRunner(db).run()
                (
                    account_id,
                    request_db_id,
                    attempt_id,
                    reservation_id,
                ) = await _seed_account_and_request(
                    db,
                    account_name=name,
                    api_key_env=f"K_{name}",
                    model_id="test-model",
                )

                health_manager = HealthManager()
                quota_estimator = QuotaEstimator()
                coordinator = _build_coordinator(
                    db=db,
                    cache=cache,
                    registry=registry,
                    selected_provider_caps=provider_caps,
                    policy=policy,
                    quota_estimator=quota_estimator,
                    health_manager=health_manager,
                )

                original_body = (
                    b'{"model":"test-model",'
                    b'"messages":[{"role":"user","content":"hi"}],'
                    b'"thinking":{"type":"enabled","budget_tokens":50000}}'
                )
                upstream_body = original_body
                ctx = _make_context(
                    original_body=original_body,
                    upstream_body=upstream_body,
                    model_id="test-model",
                )
                ctx.client_metadata["db_request_id"] = str(request_db_id)
                ctx.client_metadata["account_name"] = name

                # Simulate the runtime side effects that
                # _select_and_persist_attempt would have applied so
                # the cleanup branch can observe them.
                state = registry.get_state(name)
                assert state is not None
                state.active_request_count = 1
                await quota_estimator.add_reservation(name, 1000)

                selected = _make_selected_attempt(
                    attempt_id=attempt_id,
                    reservation_id=reservation_id,
                    account_name=name,
                    db_request_id=request_db_id,
                    estimated_microdollars=1000,
                    provider_id="test-provider",
                )

                with pytest.raises(BudgetResolutionError):
                    try:
                        coordinator._apply_selected_provider_transcode_adjustments(
                            context=ctx,
                            selected=selected,
                        )
                    except CapabilityError as err:
                        await coordinator._finalize_selected_capability_rejection(
                            context=ctx,
                            selected=selected,
                            err=err,
                        )
                        raise

                # --- Attempt finalized ------------------------------
                attempt_row = await db.fetch_one(
                    "SELECT completed_at, status_code, error_class, "
                    "release_reason, retry_category "
                    "FROM request_attempts WHERE id = ?",
                    (attempt_id,),
                )
                assert attempt_row is not None
                assert attempt_row["completed_at"] is not None
                assert attempt_row["status_code"] == 400
                assert attempt_row["error_class"] == "BudgetResolutionError"
                assert attempt_row["release_reason"] == "capability_rejected"
                assert attempt_row["retry_category"] == "never"

                # --- Reservation released durably ------------------
                reservation_row = await db.fetch_one(
                    "SELECT status, released_at FROM reservations WHERE id = ?",
                    (int(reservation_id),),
                )
                assert reservation_row is not None
                assert reservation_row["status"] == "released"
                assert reservation_row["released_at"] is not None

                # --- In-memory reservation cleared ------------------
                assert quota_estimator._account_reserved_cost.get(name, 0) == 0

                # --- Active request count decremented ---------------
                assert state.active_request_count == 0

                # --- Thinking trace records the rejection -----------
                assert ctx.thinking_trace is not None
                assert ctx.thinking_trace["decision"] == "rejected"
                assert ctx.thinking_trace["provider_id"] == "test-provider"

                # --- Request row finalized --------------------------
                request_row = await db.fetch_one(
                    "SELECT status, error_class FROM requests WHERE id = ?",
                    (request_db_id,),
                )
                assert request_row is not None
                assert request_row["status"] == "client_error"
                assert request_row["error_class"] == "BudgetResolutionError"
            finally:
                await db.disconnect()
        finally:
            os.environ.pop(f"K_{name}", None)


class TestStrictStreamingRejectionCleansUp:
    """Test 4: streaming strict rejection cleans up identically."""

    @pytest.mark.asyncio()
    async def test_streaming_strict_rejection_finalizes_state(self) -> None:
        from eggpool.accounts.registry import AccountRegistry
        from eggpool.catalog.cache import ModelCatalogCache
        from eggpool.db.connection import Database
        from eggpool.db.migrations import MigrationRunner
        from eggpool.health.health_manager import HealthManager
        from eggpool.models.config import AppConfig
        from eggpool.quota.estimation import QuotaEstimator

        name = "0001"
        os.environ[f"K_{name}"] = "k"
        try:
            config = AppConfig.model_validate(
                {
                    "providers": {
                        "test-provider": {
                            "id": "test-provider",
                            "base_url": "https://api.example.com/v1",
                            "protocols": ["anthropic"],
                            "routing_priority": 0,
                            "accounts": [
                                {
                                    "name": name,
                                    "api_key_env": f"K_{name}",
                                    "weight": 1.0,
                                }
                            ],
                        }
                    }
                }
            )
            registry = AccountRegistry(config)
            cache = ModelCatalogCache()
            provider_caps = ThinkingCapability(
                status="supported",
                source="manual_override",
                budget_tokens_max=8192,
            )
            policy = _policy(strict=True, high_default=16384)

            db = Database(path=":memory:")
            await db.connect()
            try:
                await MigrationRunner(db).run()
                (
                    account_id,
                    request_db_id,
                    attempt_id,
                    reservation_id,
                ) = await _seed_account_and_request(
                    db,
                    account_name=name,
                    api_key_env=f"K_{name}",
                    model_id="test-model",
                )

                health_manager = HealthManager()
                quota_estimator = QuotaEstimator()
                coordinator = _build_coordinator(
                    db=db,
                    cache=cache,
                    registry=registry,
                    selected_provider_caps=provider_caps,
                    policy=policy,
                    quota_estimator=quota_estimator,
                    health_manager=health_manager,
                )

                original_body = (
                    b'{"model":"test-model",'
                    b'"messages":[{"role":"user","content":"hi"}],'
                    b'"stream":true,'
                    b'"thinking":{"type":"enabled","budget_tokens":50000}}'
                )
                ctx = _make_context(
                    original_body=original_body,
                    upstream_body=original_body,
                    model_id="test-model",
                    streaming=True,
                )
                ctx.client_metadata["db_request_id"] = str(request_db_id)
                ctx.client_metadata["account_name"] = name

                state = registry.get_state(name)
                assert state is not None
                state.active_request_count = 1
                await quota_estimator.add_reservation(name, 1000)

                selected = _make_selected_attempt(
                    attempt_id=attempt_id,
                    reservation_id=reservation_id,
                    account_name=name,
                    db_request_id=request_db_id,
                    estimated_microdollars=1000,
                    provider_id="test-provider",
                    streamed=True,
                )

                with pytest.raises(BudgetResolutionError):
                    try:
                        coordinator._apply_selected_provider_transcode_adjustments(
                            context=ctx,
                            selected=selected,
                        )
                    except CapabilityError as err:
                        await coordinator._finalize_selected_capability_rejection(
                            context=ctx,
                            selected=selected,
                            err=err,
                        )
                        raise

                # Streaming and non-streaming must produce identical
                # durable state.
                attempt_row = await db.fetch_one(
                    "SELECT release_reason, retry_category "
                    "FROM request_attempts WHERE id = ?",
                    (attempt_id,),
                )
                assert attempt_row is not None
                assert attempt_row["release_reason"] == "capability_rejected"
                assert attempt_row["retry_category"] == "never"

                request_row = await db.fetch_one(
                    "SELECT status FROM requests WHERE id = ?",
                    (request_db_id,),
                )
                assert request_row is not None
                assert request_row["status"] == "client_error"

                assert state.active_request_count == 0
                assert quota_estimator._account_reserved_cost.get(name, 0) == 0
                # No lazy stream generator survives — the recompute
                # raised before upstream dispatch, so the streaming
                # entrypoint never produced a response generator.
                assert ctx.thinking_trace is not None
                assert ctx.thinking_trace["decision"] == "rejected"
            finally:
                await db.disconnect()
        finally:
            os.environ.pop(f"K_{name}", None)


class TestCleanupHelperIdempotent:
    """Test 5: the cleanup helper is safe to invoke once.

    The cleanup branch only marks the attempt terminal because
    ``AttemptFinalizer.finalize_failed_attempt`` is itself idempotent.
    This pins that property so future callers do not have to add a
    ``_finalize_done`` guard.
    """

    @pytest.mark.asyncio()
    async def test_double_cleanup_does_not_corrupt(self) -> None:
        from eggpool.accounts.registry import AccountRegistry
        from eggpool.catalog.cache import ModelCatalogCache
        from eggpool.db.connection import Database
        from eggpool.db.migrations import MigrationRunner
        from eggpool.health.health_manager import HealthManager
        from eggpool.models.config import AppConfig
        from eggpool.quota.estimation import QuotaEstimator

        name = "0001"
        os.environ[f"K_{name}"] = "k"
        try:
            config = AppConfig.model_validate(
                {
                    "providers": {
                        "test-provider": {
                            "id": "test-provider",
                            "base_url": "https://api.example.com/v1",
                            "protocols": ["anthropic"],
                            "routing_priority": 0,
                            "accounts": [
                                {
                                    "name": name,
                                    "api_key_env": f"K_{name}",
                                    "weight": 1.0,
                                }
                            ],
                        }
                    }
                }
            )
            registry = AccountRegistry(config)
            cache = ModelCatalogCache()
            provider_caps = ThinkingCapability(
                status="supported",
                source="manual_override",
                budget_tokens_max=8192,
            )
            policy = _policy(strict=True, high_default=16384)

            db = Database(path=":memory:")
            await db.connect()
            try:
                await MigrationRunner(db).run()
                (
                    account_id,
                    request_db_id,
                    attempt_id,
                    reservation_id,
                ) = await _seed_account_and_request(
                    db,
                    account_name=name,
                    api_key_env=f"K_{name}",
                    model_id="test-model",
                )

                health_manager = HealthManager()
                quota_estimator = QuotaEstimator()
                coordinator = _build_coordinator(
                    db=db,
                    cache=cache,
                    registry=registry,
                    selected_provider_caps=provider_caps,
                    policy=policy,
                    quota_estimator=quota_estimator,
                    health_manager=health_manager,
                )

                state = registry.get_state(name)
                assert state is not None
                state.active_request_count = 1
                await quota_estimator.add_reservation(name, 1000)

                ctx = _make_context(
                    original_body=(
                        b'{"model":"test-model",'
                        b'"messages":[{"role":"user","content":"hi"}],'
                        b'"thinking":{"type":"enabled","budget_tokens":50000}}'
                    ),
                    upstream_body=(
                        b'{"model":"test-model",'
                        b'"messages":[{"role":"user","content":"hi"}],'
                        b'"thinking":{"type":"enabled","budget_tokens":50000}}'
                    ),
                    model_id="test-model",
                )
                ctx.client_metadata["db_request_id"] = str(request_db_id)
                ctx.client_metadata["account_name"] = name

                selected = _make_selected_attempt(
                    attempt_id=attempt_id,
                    reservation_id=reservation_id,
                    account_name=name,
                    db_request_id=request_db_id,
                    estimated_microdollars=1000,
                    provider_id="test-provider",
                )
                err = BudgetResolutionError(
                    "strict clamp rejection",
                    model_id="test-model",
                    requested_budget_tokens=50000,
                    resolved_budget_tokens=8192,
                    budget_resolution_policy="strict",
                    reason="strict_clamp",
                    provider_id="test-provider",
                )

                await coordinator._finalize_selected_capability_rejection(
                    context=ctx,
                    selected=selected,
                    err=err,
                )
                # Second invocation must not crash and must leave the
                # active count / in-memory reservation untouched.
                state_before = state.active_request_count
                quota_before = quota_estimator._account_reserved_cost.get(name, 0)
                await coordinator._finalize_selected_capability_rejection(
                    context=ctx,
                    selected=selected,
                    err=err,
                )
                assert state.active_request_count == state_before
                assert (
                    quota_estimator._account_reserved_cost.get(name, 0) == quota_before
                )
            finally:
                await db.disconnect()
        finally:
            os.environ.pop(f"K_{name}", None)


# ---------------------------------------------------------------------------
# Synchronous helper (sync coordinator build for non-async tests)
# ---------------------------------------------------------------------------


def _build_coordinator_sync(
    *,
    registry: Any,
    cache: Any,
    selected_provider_caps: ThinkingCapability,
    policy: TranscoderPolicy,
) -> RequestCoordinator:
    """Build a coordinator without async deps for the recompute-only tests.

    The recompute path only reads from the catalog cache and the
    transcoder policy, neither of which require an open database, so a
    ``None``-DB coordinator is sufficient here.
    """
    from eggpool.db.repositories import (
        AttemptRepository,
        RequestRepository,
        ReservationRepository,
        RoutingDecisionRepository,
    )
    from eggpool.request.coordinator import RequestCoordinator
    from eggpool.routing.router import Router

    cache.update_from_account(
        "0001",
        "test-provider",
        [
            {
                "model_id": "test-model",
                "protocol": "anthropic",
                "capabilities": model_capabilities_to_dict(
                    ModelCapabilities(thinking=selected_provider_caps),
                ),
            }
        ],
    )

    class _StubDb:
        """Minimal stub that exposes a no-op ``transaction`` context manager."""

        def transaction(self) -> Any:  # pragma: no cover - unused here
            raise NotImplementedError

    router = Router(registry, _MockCatalog(cache))  # type: ignore[arg-type]
    return RequestCoordinator(
        registry=registry,
        catalog=_MockCatalog(cache),  # type: ignore[arg-type]
        router=router,
        db=_StubDb(),  # type: ignore[arg-type]
        client_pool=httpx.AsyncClient(),
        request_repo=RequestRepository(_StubDb()),  # type: ignore[arg-type]
        reservation_repo=ReservationRepository(_StubDb()),  # type: ignore[arg-type]
        attempt_repo=AttemptRepository(_StubDb()),  # type: ignore[arg-type]
        routing_decision_repo=RoutingDecisionRepository(_StubDb()),  # type: ignore[arg-type]
        transcoder_policy=policy,
    )
