"""Reusable real-runtime test fixture for Eggpool integration tests.

Provides a small factory :func:`build_runtime_app` that wires up the
actual Eggpool ASGI application with a temporary file-backed SQLite
database, migrations applied, upstream interception via respx, and clean
shutdown.  The :func:`real_runtime_app` pytest fixture delegates to this
factory.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
import pytest_asyncio

from eggpool.accounts.registry import AccountRegistry
from eggpool.app import create_app
from eggpool.catalog.service import CatalogService
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import (
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
    UsageWindowRepository,
)
from eggpool.health.health_manager import HealthManager
from eggpool.models.config import AppConfig
from eggpool.proxy.client import HOP_BY_HOP_HEADERS, LOCAL_CREDENTIAL_HEADERS
from eggpool.request.coordinator import RequestCoordinator
from eggpool.request.finalization_job import RequestFinalizationSupervisor
from eggpool.routing.router import Router
from eggpool.runtime_manager import (
    ImmutableRequestState,
    RuntimeGeneration,
    RuntimeManager,
    attach_runtime_manager,
)
from eggpool.stats import StatsService

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    import pytest
    from fastapi import FastAPI

UPSTREAM_BASE = "https://real-runtime-upstream.example.com"


# ---------------------------------------------------------------------------
# Spec: describes what the factory should wire up
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """A model to seed into the catalog cache."""

    model_id: str
    protocol: str = "openai"
    capabilities: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    """A provider with static models and account bindings."""

    provider_id: str
    base_url: str = UPSTREAM_BASE
    protocols: tuple[str, ...] = ("openai",)
    static_models: tuple[ModelSpec, ...] = ()
    account_names: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RuntimeAppSpec:
    """Describes a runtime app to build.

    Call :func:`build_runtime_app` with a spec to get a fully wired
    ``FastAPI`` application.
    """

    account_names: tuple[str, ...] = ("rt-acct-1", "rt-acct-2")
    models: tuple[ModelSpec, ...] = (ModelSpec(model_id="gpt-4", protocol="openai"),)
    providers: tuple[ProviderSpec, ...] = ()
    transcoder_overrides: dict[str, Any] = field(default_factory=dict)


# Default spec matching the original real_runtime_app behavior
DEFAULT_SPEC = RuntimeAppSpec()


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _build_config_from_spec(
    spec: RuntimeAppSpec,
    tmp_db: str,
) -> AppConfig:
    """Build an AppConfig from a RuntimeAppSpec."""
    accounts = [
        {"name": name, "api_key_env": "REAL_RUNTIME_KEY"} for name in spec.account_names
    ]

    providers_dict: dict[str, Any] = {}
    for prov in spec.providers:
        static_models_list = [
            {"id": m.model_id, "protocol": m.protocol} for m in prov.static_models
        ]
        providers_dict[prov.provider_id] = {
            "id": prov.provider_id,
            "base_url": prov.base_url,
            "protocols": list(prov.protocols),
            "static_models": static_models_list,
            "accounts": [
                {
                    "name": name,
                    "api_key_env": "REAL_RUNTIME_KEY",
                    "enabled": True,
                    "weight": 1.0,
                }
                for name in prov.account_names
            ],
        }

    config_dict: dict[str, Any] = {
        "server": {
            "api_key_env": "REAL_RUNTIME_KEY",
            "host": "127.0.0.1",
            "port": 0,
        },
        "database": {"path": tmp_db},
        "upstream": {"base_url": UPSTREAM_BASE},
        "models": {"startup_refresh": False, "refresh_interval_s": 0},
        "accounts": accounts,
        "dashboard": {"enabled": False},
    }
    if spec.transcoder_overrides:
        config_dict["transcoder"] = spec.transcoder_overrides
    if providers_dict:
        config_dict["providers"] = providers_dict

    return AppConfig.from_dict(config_dict)


@dataclass(slots=True)
class RuntimeAppResult:
    """Result of :func:`build_runtime_app`.  Holds all wired components."""

    application: FastAPI
    db: Database
    httpx_client: httpx.AsyncClient
    registry: AccountRegistry
    catalog: CatalogService
    router: Router
    health_manager: HealthManager
    coordinator: RequestCoordinator
    runtime_manager: RuntimeManager


class _TestGenerationSupervisor:
    """Minimal generation task owner for manually wired request tests."""

    def all_healthy(self) -> bool:
        return True

    async def stop_all(self) -> None:
        return


async def install_test_runtime_manager(
    application: FastAPI,
    *,
    config: AppConfig,
    db: Database,
    registry: AccountRegistry,
    catalog: CatalogService,
    router: Router,
    coordinator: RequestCoordinator,
    client_pool: Any,
) -> RuntimeManager:
    """Install the canonical runtime boundary around a test coordinator.

    Hand-built integration fixtures use real request-path services but do
    not run the production lifespan. This helper gives those tests the same
    runtime-manager and terminal-supervisor contract as production.
    """
    finalization_supervisor = RequestFinalizationSupervisor(
        db=db,
        effects_applier=coordinator._effects_applier,  # pyright: ignore[reportPrivateUsage]
    )
    coordinator._finalization_supervisor = finalization_supervisor  # pyright: ignore[reportPrivateUsage]
    generation = RuntimeGeneration(
        generation_id=0,
        config=config,
        config_digest="test",
        registry=registry,
        catalog=catalog,
        router=router,
        coordinator=coordinator,
        client_pool=client_pool,
        outbound_manager=None,
        health_manager=coordinator._health_manager,  # pyright: ignore[reportPrivateUsage]
        cost_calculator=coordinator._cost_calculator,  # pyright: ignore[reportPrivateUsage]
        transcoder_policy=coordinator._transcoder_policy,  # pyright: ignore[reportPrivateUsage]
        compression_policy=coordinator._compression_policy,  # pyright: ignore[reportPrivateUsage]
        dispatch_overhead_recorder=coordinator._dispatch_overhead_recorder,  # pyright: ignore[reportPrivateUsage]
        dispatch_span_recorder=coordinator._dispatch_span_recorder,  # pyright: ignore[reportPrivateUsage]
        account_backoff_repo=coordinator._account_backoff_repo,  # pyright: ignore[reportPrivateUsage]
        stats_service=getattr(application.state, "stats", None),
        supervisor=_TestGenerationSupervisor(),
        routing_trace_guard=coordinator._routing_trace_guard,  # pyright: ignore[reportPrivateUsage]
        routing_trace_writer=coordinator._routing_trace_writer,  # pyright: ignore[reportPrivateUsage]
        effects_applier=coordinator._effects_applier,  # pyright: ignore[reportPrivateUsage]
        finalization_supervisor=finalization_supervisor,
        local_pre_upstream_recorder=coordinator._local_pre_upstream_recorder,  # pyright: ignore[reportPrivateUsage]
        stream_diagnostics=coordinator._stream_diagnostics,  # pyright: ignore[reportPrivateUsage]
        created_at_monotonic=time.monotonic(),
        created_at_epoch=time.time(),
        immutable_request_state=ImmutableRequestState(
            provider_ids=frozenset(registry.get_provider_ids()),
            account_names=frozenset(
                state.name for state in registry.get_enabled_states()
            ),
            hop_by_hop_headers=HOP_BY_HOP_HEADERS,
            local_credential_headers=LOCAL_CREDENTIAL_HEADERS,
        ),
    )
    manager = RuntimeManager()
    await manager.install_initial(generation)
    attach_runtime_manager(application, manager)
    return manager


async def build_runtime_app(
    spec: RuntimeAppSpec = DEFAULT_SPEC,
    *,
    tmp_path: Any,
    env_key: str = "REAL_RUNTIME_KEY",
    env_value: str = "rt-test-key",
) -> RuntimeAppResult:
    """Wire up and return a fully configured Eggpool runtime app.

    This is the single source of truth for test-fixture component wiring.
    Both the :func:`real_runtime_app` fixture and specialized fixtures
    (e.g. MiniMax isolation) should delegate here.
    """
    config = _build_config_from_spec(spec, tmp_db=str(tmp_path / "test.db"))
    application = create_app(config)

    db = Database(path=str(tmp_path / "test.db"))
    await db.connect()
    application.state.db = db

    runner = MigrationRunner(db)
    await runner.run()

    async with db.transaction():
        for name in spec.account_names:
            await db.execute_write(
                "INSERT INTO accounts (name, api_key_env, enabled, weight) "
                "VALUES (?, ?, 1, 1.0)",
                (name, env_key),
            )
        for model in spec.models:
            await db.execute_write(
                "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
                (model.model_id, model.protocol),
            )

    httpx_client = httpx.AsyncClient(
        base_url=config.upstream.base_url,
        timeout=httpx.Timeout(
            config.upstream.read_timeout_s,
            connect=config.upstream.connect_timeout_s,
            read=config.upstream.read_timeout_s,
            write=config.upstream.write_timeout_s,
            pool=config.upstream.keepalive_timeout_s,
        ),
        limits=httpx.Limits(
            max_connections=config.upstream.max_connections,
            max_keepalive_connections=config.upstream.max_keepalive,
            keepalive_expiry=config.upstream.keepalive_timeout_s,
        ),
    )
    application.state.httpx_client = httpx_client

    registry = AccountRegistry(config)
    application.state.registry = registry

    catalog = CatalogService(config, registry, db, httpx_client)
    application.state.catalog = catalog

    router = Router(registry, catalog)
    application.state.router = router

    application.state.stats = StatsService(db)

    health_manager = HealthManager()
    application.state.health_manager = health_manager

    request_repo = RequestRepository(db)
    reservation_repo = ReservationRepository(db)
    attempt_repo = AttemptRepository(db)
    usage_window_repo = UsageWindowRepository(db)

    coordinator = RequestCoordinator(
        registry=registry,
        catalog=catalog,
        router=router,
        db=db,
        client_pool=httpx_client,
        request_repo=request_repo,
        reservation_repo=reservation_repo,
        attempt_repo=attempt_repo,
        usage_window_repo=usage_window_repo,
        health_manager=health_manager,
        transcoder_policy=config.transcoder,
    )
    runtime_manager = await install_test_runtime_manager(
        application,
        config=config,
        db=db,
        registry=registry,
        catalog=catalog,
        router=router,
        coordinator=coordinator,
        client_pool=httpx_client,
    )

    # Seed catalog with models
    for model in spec.models:
        catalog.cache.load_model(
            model_id=model.model_id,
            display_name=model.model_id,
            protocol=model.protocol,
            capabilities=model.capabilities,
            source_metadata={},
        )
        for name in spec.account_names:
            catalog.cache.add_account_support(model.model_id, name)

    # Seed provider-model entries
    for prov in spec.providers:
        for name in prov.account_names:
            if name in spec.account_names:
                catalog.cache.set_account_provider(name, prov.provider_id)
                catalog.cache.update_from_account(
                    name,
                    prov.provider_id,
                    [
                        {
                            "model_id": m.model_id,
                            "display_name": m.model_id,
                            "protocol": m.protocol,
                            "capabilities": m.capabilities,
                            "source_metadata": {},
                        }
                        for m in prov.static_models
                    ],
                )

    return RuntimeAppResult(
        application=application,
        db=db,
        httpx_client=httpx_client,
        registry=registry,
        catalog=catalog,
        router=router,
        health_manager=health_manager,
        coordinator=coordinator,
        runtime_manager=runtime_manager,
    )


# ---------------------------------------------------------------------------
# Pytest fixture (delegates to factory)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def real_runtime_app(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[FastAPI, None]:
    """Provide an actual Eggpool ASGI application with real components."""
    monkeypatch.setenv("REAL_RUNTIME_KEY", "rt-test-key")
    result = await build_runtime_app(tmp_path=tmp_path)
    try:
        yield result.application
    finally:
        # The runtime manager owns all generation tasks that can use the
        # database.  Join those tasks before closing the fixture-owned
        # connection, matching the production lifespan boundary.
        await result.runtime_manager.shutdown()
        assert result.runtime_manager.retirement_snapshot() == ()
        await result.db.disconnect()
        assert result.db._conn is None  # noqa: SLF001
        await result.httpx_client.aclose()
