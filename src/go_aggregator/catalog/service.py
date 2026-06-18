"""Catalog service: orchestrates model discovery and refresh."""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
from typing import TYPE_CHECKING, Any

from go_aggregator.catalog.cache import ModelCatalogCache
from go_aggregator.catalog.fetcher import fetch_models_for_account
from go_aggregator.catalog.normalizer import normalize_models
from go_aggregator.catalog.pricing import (
    parse_microdollars_per_million,
    parse_price_per_1k,
)
from go_aggregator.catalog.protocols import ModelProtocolResolver
from go_aggregator.db.repositories import PriceSnapshotRepository
from go_aggregator.providers.client_pool import ProviderClientPool

if TYPE_CHECKING:
    import httpx

    from go_aggregator.accounts.registry import AccountRegistry
    from go_aggregator.db.connection import Database
    from go_aggregator.health.health_manager import HealthManager
    from go_aggregator.models.config import AppConfig

logger = logging.getLogger(__name__)


def _ts_to_unix(value: object) -> float:
    """Convert a DB TIMESTAMP string (or numeric) to a Unix float.

    Returns 0.0 for None or unparseable values so cache loads never
    fail on a malformed timestamp.

    Naive datetime strings are treated as UTC (matching SQLite's
    CURRENT_TIMESTAMP convention).
    """
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    try:
        dt = _dt.datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.UTC)
        return dt.timestamp()
    except ValueError:
        try:
            dt = _dt.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
            dt = dt.replace(tzinfo=_dt.UTC)
            return dt.timestamp()
        except ValueError:
            return 0.0


class CatalogService:
    """Manages the model catalog lifecycle."""

    def __init__(
        self,
        config: AppConfig,
        registry: AccountRegistry,
        db: Database,
        client_pool: ProviderClientPool | httpx.AsyncClient,
    ) -> None:
        self._config = config
        self._registry = registry
        self._db = db
        if isinstance(client_pool, ProviderClientPool):
            self._client_pool: ProviderClientPool | None = client_pool
            if "opencode-go" in client_pool.providers:
                self._httpx_client: httpx.AsyncClient | None = client_pool.get_client(
                    "opencode-go"
                )
            else:
                self._httpx_client = None
        else:
            self._client_pool = None
            self._httpx_client = client_pool
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
            tasks: list[asyncio.Task[None]] = []
            for state in enabled_accounts:
                api_key = self._registry.get_api_key(state.name)
                if not api_key:
                    continue
                provider_id = self._registry.get_provider_for_account(state.name)
                if provider_id is None:
                    continue

                # Get provider-specific client
                client: httpx.AsyncClient | None = None
                if self._client_pool is not None:
                    try:
                        client = self._client_pool.get_client(provider_id)
                    except Exception:
                        logger.warning("No client for provider %r", provider_id)
                        continue
                elif self._httpx_client is not None:
                    client = self._httpx_client

                if client is None:
                    continue

                # Get provider config
                provider_cfg = self._config.providers.get(provider_id)
                models_method = "GET"
                models_path = "/models"
                if provider_cfg is not None:
                    models_method = provider_cfg.models_method
                    models_path = provider_cfg.models_path

                tasks.append(
                    asyncio.create_task(
                        self._fetch_and_process_account(
                            state.name,
                            api_key,
                            provider_id,
                            client,
                            models_method,
                            models_path,
                        )
                    )
                )

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            # Persist the updated catalog
            await self._persist_catalog()

            logger.info(
                "Catalog refresh complete: %d models from %d accounts",
                self._cache.model_count,
                len(enabled_accounts),
            )

    async def _fetch_and_process_account(
        self,
        account_name: str,
        api_key: str,
        provider_id: str,
        client: httpx.AsyncClient,
        models_method: str = "GET",
        models_path: str = "/models",
    ) -> None:
        """Fetch models for one account and update the cache."""
        try:
            raw_response = await fetch_models_for_account(
                client,
                api_key,
                account_name,
                models_method=models_method,
                models_path=models_path,
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
                        # Fall back to catalog resolution, using persisted
                        # protocol as a fallback so previously-resolved
                        # models do not become unresolved on refresh.
                        persisted = self._cache.get_model(model["model_id"])
                        persisted_protocol = (
                            persisted.get("protocol") if persisted else None
                        )
                        resolution = self._protocol_resolver.resolve_from_catalog(
                            model["model_id"],
                            persisted_protocol=persisted_protocol,
                        )
                        if resolution.protocol:
                            model["protocol"] = resolution.protocol
                            model["protocol_source"] = resolution.source

            # Enforce provider protocol constraints: clear protocol for
            # models whose resolved protocol is not supported by this
            # provider so they are not routed through an incompatible
            # upstream.
            provider_cfg = self._config.providers.get(provider_id)
            if provider_cfg is not None:
                supported = set(provider_cfg.protocols)
                for model in models:
                    proto = model.get("protocol")
                    if proto and proto not in supported:
                        logger.debug(
                            "Clearing unsupported protocol %r for %s on provider %s",
                            proto,
                            model["model_id"],
                            provider_id,
                        )
                        model["protocol"] = None
                        model["protocol_source"] = "provider_mismatch"

            self._cache.update_from_account(account_name, provider_id, models)
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
                caps: dict[str, Any] = json.loads(caps_raw) if caps_raw else {}
                meta_raw = row["source_metadata"]
                meta: dict[str, Any] = json.loads(meta_raw) if meta_raw else {}
                self._cache.load_model(
                    model_id=model_id,
                    display_name=row["display_name"],
                    protocol=row["protocol"],
                    capabilities=caps,
                    source_metadata=meta,
                    protocol_source=row["protocol_source"],
                    first_seen_at=_ts_to_unix(row["first_seen_at"]),
                    last_seen_at=_ts_to_unix(row["last_seen_at"]),
                )

            # Load account-model relationships with provider info
            am_rows = await self._db.fetch_all(
                "SELECT account_id, model_id FROM account_models WHERE enabled = 1"
            )
            # Build account name lookup
            acct_rows = await self._db.fetch_all(
                "SELECT id, name, provider_id FROM accounts"
            )
            id_to_name = {row["id"]: row["name"] for row in acct_rows}
            id_to_provider: dict[int, str] = {
                row["id"]: row["provider_id"] for row in acct_rows
            }

            for row in am_rows:
                model_id = row["model_id"]
                account_name = id_to_name.get(row["account_id"])
                provider_id = id_to_provider.get(row["account_id"], "opencode-go")
                if account_name:
                    self._cache.set_account_provider(account_name, provider_id)
                    if self._cache.has_model(model_id):
                        self._cache.add_account_support(model_id, account_name)
                        # Populate per-provider metadata from global entry
                        # so provider-suffixed exposure works before the
                        # next live refresh.
                        provider_key = (model_id, provider_id)
                        if provider_key not in self._cache.get_provider_model_entries():
                            model_info = self._cache.get_model(model_id)
                            if model_info is not None:
                                self._cache.set_provider_model_entry(
                                    model_id, provider_id, dict(model_info)
                                )

            if self._cache.model_count > 0:
                logger.info(
                    "Loaded %d cached models from database",
                    self._cache.model_count,
                )
            self._cache.hydrate_account_refresh_ages()
            self._cache.hydrate_refresh_age()
        except Exception:
            logger.exception("Failed to load cached models")
            raise

    async def _persist_catalog(self) -> None:
        """Persist the in-memory catalog to the database."""
        now_sql = "datetime('now')"

        async with self._db.transaction():
            for model_id, model_info in self._cache.get_all_models().items():
                protocol = model_info.get("protocol")
                if protocol not in ("openai", "anthropic"):
                    logger.warning(
                        "Skipping unresolved model during catalog persistence: %s",
                        model_id,
                    )
                    continue

                # Use preserved protocol source from resolution
                protocol_source = model_info.get("protocol_source")
                if protocol_source == "unresolved":
                    protocol_source = None

                # Upsert model
                await self._db.execute_write(
                    f"""
                    INSERT INTO models (
                        model_id, display_name, protocol,
                        capabilities, source_metadata,
                        first_seen_at, last_seen_at, protocol_source,
                        resolution_status
                    ) VALUES (?, ?, ?, ?, ?, {now_sql}, {now_sql}, ?, 'resolved')
                    ON CONFLICT(model_id) DO UPDATE SET
                        display_name = excluded.display_name,
                        protocol = excluded.protocol,
                        capabilities = excluded.capabilities,
                        source_metadata = excluded.source_metadata,
                        last_seen_at = {now_sql},
                        protocol_source = excluded.protocol_source,
                        resolution_status = 'resolved'
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

                    await self._db.execute_write(
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

            # 7.6: Insert per-provider price snapshots
            for (
                pid_model_id,
                pid,
            ), pinfo in self._cache.get_provider_model_entries().items():
                if self._cache.has_model(pid_model_id):
                    await self._maybe_insert_price_snapshot(
                        pid_model_id, pinfo, provider_id=pid
                    )

    async def _maybe_insert_price_snapshot(
        self,
        model_id: str,
        model_info: dict[str, Any],
        provider_id: str = "opencode-go",
    ) -> None:
        """Insert a price snapshot if pricing data is available.

        Each pricing category (input, output, cache-read, cache-write)
        is resolved independently: a TOML override is authoritative
        for any category it sets, and any category not set by TOML
        falls back to upstream metadata. The resulting ``source`` is

        - ``"config"`` if every present value came from TOML,
        - ``"upstream"`` if every present value came from upstream
          metadata,
        - ``"mixed"`` if the present values came from a mix of both.

        The snapshot is only inserted when its values differ from the
        latest snapshot for the same model.
        """
        # Per-category override values (None means: no override for this
        # category, fall back to upstream).
        override = self._config.model_overrides.get(model_id)
        override_values: dict[str, Any] = {}
        if override is not None:
            if override.input_price_per_1k is not None:
                override_values["input"] = override.input_price_per_1k
            if override.output_price_per_1k is not None:
                override_values["output"] = override.output_price_per_1k
            if override.cache_read_per_million_microdollars is not None:
                override_values["cache_read"] = (
                    override.cache_read_per_million_microdollars
                )
            if override.cache_write_per_million_microdollars is not None:
                override_values["cache_write"] = (
                    override.cache_write_per_million_microdollars
                )

        meta: dict[str, Any] = model_info.get("source_metadata", {})

        def _safe_parse_price_per_1k(
            category: str, value: object, *, default_unit: str = "1k"
        ) -> float | None:
            try:
                return parse_price_per_1k(value, default_unit=default_unit)
            except ValueError as exc:
                logger.warning(
                    "Ignoring invalid %s price for %s: %s",
                    category,
                    model_id,
                    exc,
                )
                return None

        def _safe_parse_microdollars(category: str, value: object) -> int | None:
            try:
                return parse_microdollars_per_million(value)
            except ValueError as exc:
                logger.warning(
                    "Ignoring invalid %s price for %s: %s",
                    category,
                    model_id,
                    exc,
                )
                return None

        def _resolve_input() -> float | None:
            if "input" in override_values:
                return override_values["input"]
            pricing: dict[str, Any] | None = meta.get("pricing")
            if isinstance(pricing, dict) and "prompt" in pricing:
                return _safe_parse_price_per_1k(
                    "input", pricing["prompt"], default_unit="token"
                )
            upstream = meta.get("input_price_per_1k")
            return _safe_parse_price_per_1k("input", upstream)

        def _resolve_output() -> float | None:
            if "output" in override_values:
                return override_values["output"]
            pricing: dict[str, Any] | None = meta.get("pricing")
            if isinstance(pricing, dict) and "completion" in pricing:
                return _safe_parse_price_per_1k(
                    "output", pricing["completion"], default_unit="token"
                )
            upstream = meta.get("output_price_per_1k")
            return _safe_parse_price_per_1k("output", upstream)

        def _resolve_cache_read() -> int | None:
            if "cache_read" in override_values:
                return int(override_values["cache_read"])
            upstream = meta.get("cache_read_per_million_microdollars")
            return _safe_parse_microdollars("cache_read", upstream)

        def _resolve_cache_write() -> int | None:
            if "cache_write" in override_values:
                return int(override_values["cache_write"])
            upstream = meta.get("cache_write_per_million_microdollars")
            return _safe_parse_microdollars("cache_write", upstream)

        input_price = _resolve_input()
        output_price = _resolve_output()
        cache_read_price = _resolve_cache_read()
        cache_write_price = _resolve_cache_write()

        if all(
            value is None
            for value in (
                input_price,
                output_price,
                cache_read_price,
                cache_write_price,
            )
        ):
            return

        # Determine the per-category provenance to compute the snapshot
        # source. A category is considered "present" if it has a value;
        # its provenance is "config" if the override provided it,
        # otherwise "upstream".
        present_provenance: set[str] = set()
        for _key, override_key, present in (
            ("input", "input", input_price is not None),
            ("output", "output", output_price is not None),
            ("cache_read", "cache_read", cache_read_price is not None),
            ("cache_write", "cache_write", cache_write_price is not None),
        ):
            if not present:
                continue
            present_provenance.add(
                "config" if override_key in override_values else "upstream"
            )

        if present_provenance == {"config"}:
            source = "config"
        elif present_provenance == {"upstream"}:
            source = "upstream"
        else:
            source = "mixed"

        # Skip insert when every field already matches the latest snapshot.
        snapshot_repo = PriceSnapshotRepository(self._db)
        latest = await snapshot_repo.get_latest(model_id)
        if latest is not None:
            old_input = latest.get("input_price_per_1k")
            old_output = latest.get("output_price_per_1k")
            old_cache_read = latest.get("cache_read_per_million_microdollars")
            old_cache_write = latest.get("cache_write_per_million_microdollars")
            old_source = latest.get("source")
            old_provider = latest.get("provider_id", "opencode-go")
            if (
                old_input == input_price
                and old_output == output_price
                and old_cache_read == cache_read_price
                and old_cache_write == cache_write_price
                and old_source == source
                and old_provider == provider_id
            ):
                return  # No change, skip insert

        # Insert new snapshot. Cache rates are always int microdollars
        # here; legacy float input/output are forwarded to the repo's
        # auto-conversion path.
        await snapshot_repo.record(
            model_id,
            input_price_per_1k=input_price,
            output_price_per_1k=output_price,
            cache_read_per_million_microdollars=cache_read_price,
            cache_write_per_million_microdollars=cache_write_price,
            source=source,
            provider_id=provider_id,
        )
        logger.debug(
            "Inserted price snapshot for %s: input=%s output=%s "
            "cache_read=%s cache_write=%s source=%s",
            model_id,
            input_price,
            output_price,
            cache_read_price,
            cache_write_price,
            source,
        )

    def get_models_for_exposure(
        self,
        health_manager: HealthManager | None = None,
    ) -> list[dict[str, Any]]:
        """Get models to expose via /v1/models with provider-suffixed IDs."""
        eligible = {s.name for s in self._registry.get_eligible_states()}
        if self._config.models.expose_mode == "healthy_union" and health_manager:
            eligible = {
                name for name in eligible if health_manager.is_account_healthy(name)
            }
        return self._cache.get_provider_suffixed_models(
            self._config.models.expose_mode,
            eligible,
        )

    def is_model_available(self, model_id: str) -> bool:
        """Check if a model is available from any eligible account."""
        eligible = {s.name for s in self._registry.get_eligible_states()}
        return self._cache.is_model_available(model_id, eligible)
