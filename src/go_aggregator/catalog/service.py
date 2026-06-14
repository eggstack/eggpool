"""Catalog service: orchestrates model discovery and refresh."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from go_aggregator.catalog.cache import ModelCatalogCache
from go_aggregator.catalog.fetcher import fetch_models_for_account
from go_aggregator.catalog.normalizer import normalize_models
from go_aggregator.catalog.protocols import ModelProtocolResolver
from go_aggregator.db.repositories import PriceSnapshotRepository

if TYPE_CHECKING:
    import httpx

    from go_aggregator.accounts.registry import AccountRegistry
    from go_aggregator.db.connection import Database
    from go_aggregator.models.config import AppConfig

logger = logging.getLogger(__name__)


class CatalogService:
    """Manages the model catalog lifecycle."""

    def __init__(
        self,
        config: AppConfig,
        registry: AccountRegistry,
        db: Database,
        httpx_client: httpx.AsyncClient,
    ) -> None:
        self._config = config
        self._registry = registry
        self._db = db
        self._httpx_client = httpx_client
        self._cache = ModelCatalogCache()
        self._refresh_lock = asyncio.Lock()
        self._protocol_resolver = ModelProtocolResolver(config)

    @property
    def cache(self) -> ModelCatalogCache:
        return self._cache

    async def refresh(self) -> None:
        """Fetch models from all enabled accounts and update cache."""
        async with self._refresh_lock:
            logger.info("Starting catalog refresh")

            # Load cached models from database first
            await self._load_cached_models()

            enabled_accounts = self._registry.get_enabled_states()
            if not enabled_accounts:
                logger.warning("No enabled accounts for catalog refresh")
                return

            # Fetch concurrently for each account
            tasks = []
            for state in enabled_accounts:
                api_key = self._registry.get_api_key(state.name)
                if not api_key:
                    continue
                tasks.append(self._fetch_and_process_account(state.name, api_key))

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            # Persist the updated catalog
            await self._persist_catalog()

            logger.info(
                "Catalog refresh complete: %d models from %d accounts",
                self._cache.model_count,
                len(enabled_accounts),
            )

    async def _fetch_and_process_account(self, account_name: str, api_key: str) -> None:
        """Fetch models for one account and update the cache."""
        try:
            raw_response = await fetch_models_for_account(
                self._httpx_client,
                api_key,
                account_name,
            )
            if not raw_response:
                return

            models = normalize_models(raw_response)

            # Apply per-model protocol resolution (Section 11)
            for model in models:
                # Check config override first
                override = self._config.model_overrides.get(model["model_id"])
                if override and override.protocol:
                    model["protocol"] = override.protocol
                    model["protocol_source"] = "config"
                else:
                    # Resolve from per-model metadata
                    source_meta = model.get("source_metadata", {})
                    resolution = self._protocol_resolver.resolve_from_metadata(
                        model["model_id"], source_meta
                    )
                    if resolution.protocol:
                        model["protocol"] = resolution.protocol
                        model["protocol_source"] = resolution.source
                    else:
                        # Fall back to catalog resolution
                        resolution = self._protocol_resolver.resolve_from_catalog(
                            model["model_id"]
                        )
                        if resolution.protocol:
                            model["protocol"] = resolution.protocol
                            model["protocol_source"] = resolution.source

            self._cache.update_from_account(account_name, models)
            logger.debug(
                "Account %r: found %d models",
                account_name,
                len(models),
            )
        except Exception:
            logger.exception(
                "Failed to fetch models for account %r",
                account_name,
            )

    async def _load_cached_models(self) -> None:
        """Load previously cached models from the database."""
        try:
            rows = await self._db.fetch_all(
                "SELECT model_id, display_name, protocol, "
                "capabilities, source_metadata, "
                "first_seen_at, last_seen_at, protocol_source FROM models"
            )
            for row in rows:
                model_id = row["model_id"]
                caps_raw = row["capabilities"]
                caps = json.loads(caps_raw) if caps_raw else {}
                meta_raw = row["source_metadata"]
                meta = json.loads(meta_raw) if meta_raw else {}
                self._cache.load_model(
                    model_id=model_id,
                    display_name=row["display_name"],
                    protocol=row["protocol"],
                    capabilities=caps,
                    source_metadata=meta,
                )

            # Load account-model relationships
            am_rows = await self._db.fetch_all(
                "SELECT account_id, model_id FROM account_models WHERE enabled = 1"
            )
            # Build account name lookup
            acct_rows = await self._db.fetch_all("SELECT id, name FROM accounts")
            id_to_name = {row["id"]: row["name"] for row in acct_rows}

            for row in am_rows:
                model_id = row["model_id"]
                account_name = id_to_name.get(row["account_id"])
                if account_name and self._cache.has_model(model_id):
                    self._cache.add_account_support(model_id, account_name)

            if self._cache.model_count > 0:
                logger.info(
                    "Loaded %d cached models from database",
                    self._cache.model_count,
                )
        except Exception:
            logger.exception("Failed to load cached models")

    async def _persist_catalog(self) -> None:
        """Persist the in-memory catalog to the database."""
        now_sql = "datetime('now')"

        async with self._db.transaction():
            for model_id, model_info in self._cache.get_all_models().items():
                # Use preserved protocol source from resolution
                protocol_source = model_info.get("protocol_source")
                if protocol_source == "unresolved":
                    protocol_source = None

                # Upsert model
                await self._db.execute(
                    f"""
                    INSERT INTO models (
                        model_id, display_name, protocol,
                        capabilities, source_metadata,
                        first_seen_at, last_seen_at, protocol_source
                    ) VALUES (?, ?, ?, ?, ?, {now_sql}, {now_sql}, ?)
                    ON CONFLICT(model_id) DO UPDATE SET
                        display_name = excluded.display_name,
                        protocol = excluded.protocol,
                        capabilities = excluded.capabilities,
                        source_metadata = excluded.source_metadata,
                        last_seen_at = {now_sql},
                        protocol_source = excluded.protocol_source
                    """,
                    (
                        model_id,
                        model_info.get("display_name"),
                        model_info["protocol"],
                        json.dumps(model_info.get("capabilities", {})),
                        json.dumps(model_info.get("source_metadata", {})),
                        protocol_source,
                    ),
                )

                # Upsert account-model relationships
                supporting_accounts = self._cache.get_supporting_accounts_for_model(
                    model_id
                )
                acct_rows = await self._db.fetch_all("SELECT id, name FROM accounts")
                for acct_row in acct_rows:
                    account_id = acct_row["id"]
                    account_name = acct_row["name"]
                    is_available = 1 if account_name in supporting_accounts else 0

                    await self._db.execute(
                        f"""
                        INSERT INTO account_models (
                            account_id, model_id, enabled, created_at
                        ) VALUES (?, ?, ?, {now_sql})
                        ON CONFLICT(account_id, model_id) DO UPDATE SET
                            enabled = excluded.enabled
                        """,
                        (account_id, model_id, is_available),
                    )

                # 7.5: Insert price snapshot if pricing data is available
                await self._maybe_insert_price_snapshot(model_id, model_info)

    async def _maybe_insert_price_snapshot(
        self, model_id: str, model_info: dict[str, Any]
    ) -> None:
        """Insert a price snapshot if pricing data is available.

        Extracts pricing from TOML overrides (authoritative) or upstream
        metadata. Only inserts when values differ from the latest snapshot.
        """
        input_price: float | None = None
        output_price: float | None = None
        source = "upstream"

        # 1. Check TOML override (authoritative)
        override = self._config.model_overrides.get(model_id)
        has_override_pricing = override and (
            override.input_price_per_1k is not None
            or override.output_price_per_1k is not None
        )
        if has_override_pricing:
            input_price = override.input_price_per_1k
            output_price = override.output_price_per_1k
            source = "config"

        # 2. Check upstream source_metadata for pricing
        if input_price is None and output_price is None:
            meta = model_info.get("source_metadata", {})
            # OpenAI-style: pricing.prompt / pricing.completion (dollars per token)
            pricing = meta.get("pricing")
            if isinstance(pricing, dict):
                prompt = pricing.get("prompt")
                completion = pricing.get("completion")
                if prompt is not None:
                    input_price = float(prompt) * 1_000  # per token -> per 1k
                if completion is not None:
                    output_price = float(completion) * 1_000
            # Some providers: input_price_per_1k / output_price_per_1k
            if input_price is None:
                input_price = meta.get("input_price_per_1k")
            if output_price is None:
                output_price = meta.get("output_price_per_1k")

        if input_price is None and output_price is None:
            return

        # 3. Check if values differ from latest snapshot
        snapshot_repo = PriceSnapshotRepository(self._db)
        latest = await snapshot_repo.get_latest(model_id)
        if latest is not None:
            old_input = latest.get("input_price_per_1k")
            old_output = latest.get("output_price_per_1k")
            if old_input == input_price and old_output == output_price:
                return  # No change, skip insert

        # 4. Insert new snapshot
        await snapshot_repo.record(
            model_id,
            input_price_per_1k=input_price,
            output_price_per_1k=output_price,
            source=source,
        )
        logger.debug(
            "Inserted price snapshot for %s: input=%s output=%s source=%s",
            model_id,
            input_price,
            output_price,
            source,
        )

    def get_models_for_exposure(
        self,
    ) -> list[dict[str, Any]]:
        """Get models to expose via /v1/models."""
        eligible = {s.name for s in self._registry.get_eligible_states()}
        return self._cache.get_models_for_exposure(
            self._config.models.expose_mode,
            eligible,
        )

    def is_model_available(self, model_id: str) -> bool:
        """Check if a model is available from any eligible account."""
        eligible = {s.name for s in self._registry.get_eligible_states()}
        return self._cache.is_model_available(model_id, eligible)
