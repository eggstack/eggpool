from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

from eggpool.config_validation import ConfigValidationResult
from eggpool.control.reload_manager import ReloadManager, ReloadObserver

if TYPE_CHECKING:
    from eggpool.config_reload_policy import ReloadResult
from eggpool.models.config import (
    AccountConfig,
    AppConfig,
    ProviderConfig,
    RoutingConfig,
    ServerConfig,
)
from eggpool.runtime_manager import ProcessRuntime, RuntimeManager


def _config_digest(config: AppConfig) -> str:
    """Deterministic config digest for testing."""
    return hashlib.sha256(
        json.dumps(config.model_dump(), sort_keys=True).encode()
    ).hexdigest()


def make_initial_config() -> AppConfig:
    """Config A: single provider, two accounts, default routing.

    Differences from candidate:
    - Only one provider (test-provider-a)
    - No routing trace mode config
    - Default task intervals
    """
    return AppConfig(
        server=ServerConfig(host="127.0.0.1", port=0),
        providers={
            "test-provider-a": ProviderConfig(
                id="test-provider-a",
                base_url="https://a.example.com/v1",
                protocols=["openai"],
                accounts=[
                    AccountConfig(name="acct-a1", api_key="test-key-a1"),
                    AccountConfig(name="acct-a2", api_key="test-key-a2"),
                ],
            ),
        },
        routing=RoutingConfig(strategy="quota_fair"),
    )


def make_candidate_config() -> AppConfig:
    """Config B: two providers, three accounts, different routing.

    Observable differences from initial:
    - Added test-provider-b with one account
    - routing.strategy changed (observable in diff)
    - routing.trace.mode changed (LIVE field)
    """
    return AppConfig(
        server=ServerConfig(host="127.0.0.1", port=0),
        providers={
            "test-provider-a": ProviderConfig(
                id="test-provider-a",
                base_url="https://a.example.com/v1",
                protocols=["openai"],
                accounts=[
                    AccountConfig(name="acct-a1", api_key="test-key-a1"),
                    AccountConfig(name="acct-a2", api_key="test-key-a2"),
                ],
            ),
            "test-provider-b": ProviderConfig(
                id="test-provider-b",
                base_url="https://b.example.com/v1",
                protocols=["openai"],
                accounts=[
                    AccountConfig(name="acct-b1", api_key="test-key-b1"),
                ],
            ),
        },
        routing=RoutingConfig(strategy="quota_fair", local_quota_mode="score_only"),
    )


class ReloadHarness:
    """In-process reload test harness with real production code paths.

    Provides temporary directories, in-memory database, real config objects,
    and wired ReloadManager / RuntimeManager instances. No outbound network.

    Usage:
        async with ReloadHarness() as h:
            result = await h.reload(h.candidate_config)
            assert result.ok
    """

    def __init__(self) -> None:
        self._tmpdir: Path | None = None
        self._db = None
        self._runtime_manager: RuntimeManager | None = None
        self._process: ProcessRuntime | None = None
        self._reload_manager: ReloadManager | None = None
        self._initial_config: AppConfig | None = None
        self._candidate_config: AppConfig | None = None
        self._active_generation_id: int | None = None

    async def __aenter__(self) -> ReloadHarness:
        from eggpool.db.connection import Database
        from eggpool.db.migrations import MigrationRunner

        self._tmpdir = Path(tempfile.mkdtemp(prefix="eggpool_reload_test_"))

        # Create in-memory database with migrations
        self._db = Database(path=":memory:")
        await self._db.connect()
        await MigrationRunner(self._db).run()

        # Create configs
        self._initial_config = make_initial_config()
        self._candidate_config = make_candidate_config()

        # Wire production managers
        self._runtime_manager = RuntimeManager()
        self._process = ProcessRuntime(db=self._db, stats_db=self._db)
        self._reload_manager = ReloadManager(
            self._runtime_manager,
            self._process,
            drain_timeout_s=5.0,
        )

        # Install initial generation
        from eggpool.runtime_manager import RuntimeGenerationBuilder

        initial_digest = _config_digest(self._initial_config)
        builder = RuntimeGenerationBuilder()
        build_result = await builder.build_initial(
            self._initial_config,
            self._process,
            generation_id=0,
            config_digest=initial_digest,
            # Use MagicMock for all services in the initial generation
            registry=MagicMock(),
            catalog=MagicMock(),
            router=MagicMock(),
            coordinator=MagicMock(),
            client_pool=MagicMock(),
            outbound_manager=MagicMock(),
            dns_backend=None,
            health_manager=MagicMock(),
            cost_calculator=MagicMock(),
            transcoder_policy=MagicMock(),
            compression_policy=MagicMock(),
            cache_config=MagicMock(),
            compression_tuning_registry=MagicMock(),
            dispatch_overhead_recorder=MagicMock(),
            dispatch_span_recorder=MagicMock(),
            account_backoff_repo=MagicMock(),
            stats_service=MagicMock(),
            supervisor=MagicMock(),
            finalization_retry_queue=MagicMock(),
            routing_trace_guard=MagicMock(),
            routing_trace_writer=MagicMock(),
        )
        await self._runtime_manager.install_initial(build_result.generation)
        self._active_generation_id = build_result.generation.generation_id

        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._runtime_manager is not None:
            await self._runtime_manager.shutdown()
        if self._db is not None:
            await self._db.disconnect()
        if self._tmpdir is not None:
            import shutil

            shutil.rmtree(self._tmpdir, ignore_errors=True)

    @property
    def runtime_manager(self) -> RuntimeManager:
        assert self._runtime_manager is not None
        return self._runtime_manager

    @property
    def reload_manager(self) -> ReloadManager:
        assert self._reload_manager is not None
        return self._reload_manager

    @property
    def process(self) -> ProcessRuntime:
        assert self._process is not None
        return self._process

    @property
    def initial_config(self) -> AppConfig:
        assert self._initial_config is not None
        return self._initial_config

    @property
    def candidate_config(self) -> AppConfig:
        assert self._candidate_config is not None
        return self._candidate_config

    @property
    def db(self) -> Any:
        assert self._db is not None
        return self._db

    def make_validation(
        self,
        config: AppConfig | None = None,
        digest: str | None = None,
    ) -> ConfigValidationResult:
        """Build a ConfigValidationResult for the given config."""
        if config is None:
            config = self._candidate_config
        if digest is None:
            digest = _config_digest(config)
        return ConfigValidationResult(
            config=config,
            source_path=self._tmpdir / "config.toml"
            if self._tmpdir
            else Path("/dev/null"),
            content_digest=digest,
            runtime_fingerprint=digest,
            warnings=(),
        )

    async def reload(
        self,
        config: AppConfig | None = None,
        *,
        observer: ReloadObserver | None = None,
    ) -> ReloadResult:
        """Execute a reload with the given (or candidate) config.

        If observer is provided, replaces the reload manager's observer temporarily.
        """
        if config is None:
            config = self._candidate_config
        validation = self.make_validation(config)

        old_observer = self._reload_manager._observer
        if observer is not None:
            self._reload_manager._observer = observer
        try:
            return await self._reload_manager.reload(validation)
        finally:
            if observer is not None:
                self._reload_manager._observer = old_observer
