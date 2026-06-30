"""Catalog service: orchestrates model discovery and refresh."""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from eggpool.catalog.cache import (
    AccountCatalogOutcome,
    AccountCatalogUpdateResult,
    ModelCatalogCache,
)
from eggpool.catalog.catalog_resolvers import (
    CatalogConfig,
    CatalogHttpClient,
    CatalogResolverPipeline,
    OpenRouterCatalogResolver,
    PricingCatalogResolver,
)
from eggpool.catalog.fetcher import fetch_models_for_account
from eggpool.catalog.limits import ModelLimitResolver, extract_upstream_limits
from eggpool.catalog.normalizer import normalize_models
from eggpool.catalog.pricing_aliases import (
    PricingAliasResolver,
    seed_default_aliases,
)
from eggpool.catalog.pricing_resolver import resolve_pricing_from_metadata
from eggpool.catalog.protocols import SUPPORTED_PROTOCOLS, ModelProtocolResolver
from eggpool.constants import DEFAULT_PROVIDER_ID, DEPRECATED_MODEL_ID
from eggpool.db.repositories import (
    CatalogReconciliationRepository,
    PingRepository,
    PriceSnapshotRepository,
)
from eggpool.providers.client_pool import ProviderClientPool

if TYPE_CHECKING:
    from collections.abc import Callable

    import httpx

    from eggpool.accounts.registry import AccountRegistry
    from eggpool.db.connection import Database
    from eggpool.health.health_manager import HealthManager
    from eggpool.models.config import AppConfig, ProviderConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CatalogRefreshResult:
    """Result of a catalog refresh with diff information."""

    live_model_ids: frozenset[str]
    new_model_ids: frozenset[str]
    withdrawn_model_ids: frozenset[str]
    changed_provider_keys: frozenset[tuple[str, str]]
    refreshed_at: float
    pruned_count: int = 0


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


def _unix_to_db_timestamp(value: object, *, fallback: float) -> str:
    """Render an in-memory Unix timestamp for SQLite persistence.

    Cache hydration normalizes timestamps to Unix floats.  Keeping the inverse
    conversion at the persistence boundary prevents an unrelated catalog write
    from marking entries that were not refreshed as newly seen.
    """
    timestamp = _ts_to_unix(value)
    if timestamp <= 0:
        timestamp = fallback
    return _dt.datetime.fromtimestamp(timestamp, tz=_dt.UTC).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _parse_metadata_object(
    value: object,
    *,
    model_id: str,
    field_name: str,
) -> dict[str, Any]:
    """Parse a persisted model-metadata object without aborting hydration.

    Catalog metadata is advisory. A corrupt value must not prevent unrelated
    models and account support from loading, but it should remain observable to
    operators through a warning.
    """
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return dict(cast("dict[str, Any]", value))
    if not isinstance(value, (str, bytes, bytearray)):
        logger.warning(
            "Ignoring invalid cached %s for model %r",
            field_name,
            model_id,
        )
        return {}
    try:
        parsed: object = json.loads(value)
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning(
            "Ignoring malformed cached %s for model %r",
            field_name,
            model_id,
        )
        return {}
    if not isinstance(parsed, dict):
        logger.warning(
            "Ignoring non-object cached %s for model %r",
            field_name,
            model_id,
        )
        return {}
    return cast("dict[str, Any]", parsed)


class CatalogService:
    """Manages the model catalog lifecycle."""

    def __init__(
        self,
        config: AppConfig,
        registry: AccountRegistry,
        db: Database,
        client_pool: ProviderClientPool | httpx.AsyncClient,
        ping_repo: PingRepository | None = None,
        outbound_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config
        self._registry = registry
        self._db = db
        self._ping_repo = ping_repo
        if isinstance(client_pool, ProviderClientPool):
            self._client_pool: ProviderClientPool | None = client_pool
            self._httpx_client: httpx.AsyncClient | None = (
                client_pool.get_default_client()
            )
        else:
            self._client_pool = None
            self._httpx_client = client_pool
        # Shared outbound client for catalog resolvers when no default
        # provider client is available.  Provided by the
        # OutboundClientManager so resolvers reuse a long-lived
        # connection pool instead of constructing fresh clients.
        self._outbound_client = outbound_client
        self._cache = ModelCatalogCache()
        self._cache.set_config(config)
        self._cache_loaded = False
        self._refresh_lock = asyncio.Lock()
        self._protocol_resolver = ModelProtocolResolver(config)
        self._limit_resolver = ModelLimitResolver(config)
        self._price_change_callback: Callable[[str, str | None], None] | None = None
        # Track model_ids that have already produced an unresolved-protocol
        # warning during persistence. A persistent unresolved model would
        # otherwise spam the log every refresh cycle; the warning is the
        # signal that an upstream name needs a TOML override or family
        # mapping, not a per-cycle event.
        self._warned_unresolved_models: set[str] = set()
        # Optional external pricing catalog pipeline (OpenRouter, OpenCode Zen).
        # Populated by ``attach_pricing_resolvers`` once the database is
        # ready (catalog aliases live in ``model_pricing_aliases``).
        self._alias_resolver: PricingAliasResolver | None = None
        self._catalog_pipeline: CatalogResolverPipeline | None = None

    def _catalog_http_client(self) -> CatalogHttpClient | None:
        """Return the long-lived client used for external catalog lookups."""
        return self._outbound_client or self._httpx_client

    def set_price_change_callback(
        self, callback: Callable[[str, str | None], None]
    ) -> None:
        """Register a callback used to invalidate derived pricing caches."""
        self._price_change_callback = callback

    async def attach_pricing_resolvers(self) -> None:
        """Initialize the alias resolver + external catalog pipeline.

        Loads pricing aliases from ``model_pricing_aliases`` and wires up
        the configured external catalogs (OpenRouter, OpenCode Zen) in
        priority order. Idempotent — repeated calls refresh the alias
        registry in place.
        """
        await seed_default_aliases(self._db)
        resolver = PricingAliasResolver(self._db)
        await resolver.refresh()
        self._alias_resolver = resolver

        catalogs_config = self._config.pricing.catalogs
        resolvers: list[PricingCatalogResolver] = []
        catalog_client = self._catalog_http_client()
        for name, entry in (
            ("openrouter", catalogs_config.openrouter),
            ("opencode_zen", catalogs_config.opencode_zen),
        ):
            if not entry.enabled:
                continue
            if catalog_client is None:
                logger.warning(
                    "Skipping external pricing catalog %r: no shared outbound "
                    "HTTP client is available",
                    name,
                )
                continue
            catalog_config = CatalogConfig(
                name=name,
                enabled=entry.enabled,
                priority=entry.priority,
                ttl_seconds=entry.ttl_seconds,
                max_entries=entry.max_entries,
                base_url=entry.base_url,
                api_key=entry.api_key,
                options=entry.options,
            )
            if name == "openrouter":
                resolvers.append(
                    OpenRouterCatalogResolver(
                        config=catalog_config,
                        client=catalog_client,
                    )
                )
        if resolvers:
            self._catalog_pipeline = CatalogResolverPipeline(
                resolvers=resolvers,
                alias_resolver=resolver,
            )

    @property
    def cache(self) -> ModelCatalogCache:
        return self._cache

    async def refresh(self) -> CatalogRefreshResult:
        """Fetch models from all enabled accounts and update cache."""
        async with self._refresh_lock:
            logger.info("Starting catalog refresh")

            before_model_ids = frozenset(self._cache.get_all_models().keys())
            before_provider_keys = frozenset(
                self._cache.get_provider_model_entries().keys()
            )

            # Fresh service instances (for example ``models refresh``) need
            # durable fallback state. Long-running services load once at
            # startup and avoid rereading the full catalog every cycle.
            if not self._cache_loaded:
                await self._load_cached_models()

            enabled_accounts = self._registry.get_enabled_states()
            if not enabled_accounts:
                logger.warning("No enabled accounts for catalog refresh")
                return CatalogRefreshResult(
                    live_model_ids=frozenset(),
                    new_model_ids=frozenset(),
                    withdrawn_model_ids=frozenset(),
                    changed_provider_keys=frozenset(),
                    refreshed_at=time.time(),
                )

            # Fetch concurrently for each account
            static_tasks: list[asyncio.Task[None]] = []
            live_tasks: list[
                asyncio.Task[
                    tuple[AccountCatalogOutcome, AccountCatalogUpdateResult | None]
                ]
            ] = []
            for state in enabled_accounts:
                api_key = self._registry.get_api_key(state.name)
                provider_id = self._registry.get_provider_for_account(state.name)
                if provider_id is None:
                    continue
                if api_key is None or not self._registry.has_usable_credentials(
                    state.name
                ):
                    continue

                # Get provider-specific client
                client: httpx.AsyncClient | None = None
                if self._client_pool is not None:
                    try:
                        client = self._client_pool.get_client(provider_id, state.name)
                    except Exception:
                        logger.warning(
                            "No client for provider %r account %r",
                            provider_id,
                            state.name,
                        )
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

                # Static seeds must be ingested before the live fetch so a
                # provider with ``models_endpoint.method = "DISABLED"`` still
                # has routable rows in the cache even though the live fetch
                # returns an empty response.
                if provider_cfg is not None and provider_cfg.static_models:
                    static_tasks.append(
                        asyncio.create_task(
                            self._seed_static_models(
                                state.name,
                                provider_id,
                                provider_cfg,
                            )
                        )
                    )

                live_tasks.append(
                    asyncio.create_task(
                        self._fetch_and_process_account(
                            state.name,
                            api_key,
                            provider_id,
                            client,
                            models_method,
                            models_path,
                            provider_cfg=provider_cfg,
                        )
                    )
                )

            if static_tasks:
                await asyncio.gather(*static_tasks, return_exceptions=True)
            outcomes: list[AccountCatalogOutcome] = []
            if live_tasks:
                fetch_results = await asyncio.gather(
                    *live_tasks, return_exceptions=True
                )
                for state, fetch_result in zip(
                    enabled_accounts, fetch_results, strict=False
                ):
                    if isinstance(fetch_result, BaseException):
                        logger.warning(
                            "Catalog refresh for account %r preserved prior "
                            "support after exception: %s",
                            state.name,
                            fetch_result,
                        )
                        outcomes.append(AccountCatalogOutcome.FAILED)
                        continue
                    outcome, _update = fetch_result
                    outcomes.append(outcome)

            # Drop models no longer referenced by any account or provider
            # so the in-memory cache converges with the live catalog and
            # downstream persistence no longer needs to skip them.
            pruned = self._cache.prune_unused()
            if pruned:
                logger.info("Pruned %d stale model(s) from catalog cache", pruned)

            # Persist the updated catalog
            await self._persist_catalog()

            self._log_refresh_summary(outcomes, len(enabled_accounts))

            logger.info(
                "Catalog refresh complete: %d models from %d accounts",
                self._cache.model_count,
                len(enabled_accounts),
            )

            after_model_ids = frozenset(self._cache.get_all_models().keys())
            after_provider_keys = frozenset(
                self._cache.get_provider_model_entries().keys()
            )

            new_model_ids = after_model_ids - before_model_ids
            withdrawn_model_ids = before_model_ids - after_model_ids
            changed_provider_keys = before_provider_keys ^ after_provider_keys

            return CatalogRefreshResult(
                live_model_ids=after_model_ids,
                new_model_ids=new_model_ids,
                withdrawn_model_ids=withdrawn_model_ids,
                changed_provider_keys=changed_provider_keys,
                refreshed_at=time.time(),
                pruned_count=pruned,
            )

    def _log_refresh_summary(
        self,
        outcomes: list[AccountCatalogOutcome],
        total_accounts: int,
    ) -> None:
        """Log a per-outcome summary of the just-completed refresh cycle.

        Each non-authoritative outcome is treated as a "catalog
        uncertainty" event that preserved prior support. The default
        policy is ``preserve_until_health`` so non-destructive outcomes
        are expected; a run with many ``FAILED`` rows is still useful
        signal to operators that an upstream or DNS path is unhealthy.
        """
        if total_accounts == 0:
            return
        counts: dict[AccountCatalogOutcome, int] = {}
        for outcome in outcomes:
            counts[outcome] = counts.get(outcome, 0) + 1
        policy = self._config.models.catalog_withdrawal_policy
        logger.info(
            "Catalog refresh summary: policy=%s total=%d authoritative=%d "
            "partial=%d empty=%d failed=%d skipped=%d",
            policy,
            total_accounts,
            counts.get(AccountCatalogOutcome.SUCCESS_AUTHORITATIVE, 0),
            counts.get(AccountCatalogOutcome.SUCCESS_PARTIAL, 0),
            counts.get(AccountCatalogOutcome.SUCCESS_EMPTY, 0),
            counts.get(AccountCatalogOutcome.FAILED, 0),
            counts.get(AccountCatalogOutcome.SKIPPED, 0),
        )
        non_authoritative = sum(
            count
            for outcome, count in counts.items()
            if outcome is not AccountCatalogOutcome.SUCCESS_AUTHORITATIVE
        )
        if non_authoritative:
            logger.info(
                "Catalog refresh: %d non-authoritative outcome(s) preserved "
                "prior account/model support (policy=%s)",
                non_authoritative,
                policy,
            )

    def _build_static_models(
        self,
        provider_cfg: ProviderConfig,
        account_name: str,
    ) -> list[dict[str, Any]]:
        """Return normalized model dicts for the provider's static seeds.

        The output shape matches what ``ModelCatalogCache.update_from_account``
        expects, so static rows go through the same ingest path as live
        rows. ``account_name`` is retained for symmetry with
        ``_seed_static_models`` and for log correlation; the static seed
        itself is provider-wide.
        """
        del account_name
        if not provider_cfg.static_models:
            return []
        models: list[dict[str, Any]] = []
        for static in provider_cfg.static_models:
            capabilities: dict[str, Any] = {}
            if static.supports_tools is not None:
                capabilities["supports_tools"] = static.supports_tools
            if static.supports_vision is not None:
                capabilities["supports_vision"] = static.supports_vision
            if static.max_context_tokens is not None:
                capabilities["max_context_tokens"] = static.max_context_tokens
            if static.max_input_tokens is not None:
                capabilities["max_input_tokens"] = static.max_input_tokens
            if static.max_output_tokens is not None:
                capabilities["max_output_tokens"] = static.max_output_tokens
            source_metadata: dict[str, Any] = {
                **static.source_metadata,
                "source": "static_config",
            }
            effective = self._limit_resolver.resolve(
                provider_id=provider_cfg.id,
                model_id=static.id,
                capabilities=capabilities,
                source_metadata=source_metadata,
            )
            models.append(
                {
                    "model_id": static.id,
                    "display_name": static.display_name or static.id,
                    "protocol": static.protocol,
                    "protocol_source": "static_config" if static.protocol else None,
                    "capabilities": capabilities,
                    "source_metadata": source_metadata,
                    "discovered_limits": {
                        "context_tokens": static.max_context_tokens,
                        "input_tokens": static.max_input_tokens,
                        "output_tokens": static.max_output_tokens,
                    },
                    "effective_limits": effective.as_dict(),
                }
            )
        return models

    async def _seed_static_models(
        self,
        account_name: str,
        provider_id: str,
        provider_cfg: ProviderConfig,
    ) -> None:
        """Seed the catalog cache with the provider's static models.

        Static rows populate the cache before the live fetch so providers
        whose ``models_endpoint`` is ``DISABLED`` still have routable
        models. When the live fetch later arrives for the same account
        and model, the cache preserves explicit static
        ``protocol_source == "static_config"`` fields via
        ``ModelCatalogCache._preserve_static_fields``.
        """
        if not provider_cfg.static_models:
            return
        models = self._build_static_models(provider_cfg, account_name)
        if not models:
            return
        self._cache.set_account_provider(account_name, provider_id)
        self._cache.update_from_account(account_name, provider_id, models)
        logger.debug(
            "Seeded %d static model(s) for account %r on provider %r",
            len(models),
            account_name,
            provider_id,
        )

    async def _fetch_and_process_account(
        self,
        account_name: str,
        api_key: str,
        provider_id: str,
        client: httpx.AsyncClient,
        models_method: str = "GET",
        models_path: str = "/models",
        *,
        provider_cfg: ProviderConfig | None = None,
    ) -> tuple[AccountCatalogOutcome, AccountCatalogUpdateResult | None]:
        """Fetch models for one account and update the cache.

        Returns a tuple of ``(outcome, result)`` so the refresh cycle
        can summarize the per-account state without inspecting the cache
        directly. ``result`` is ``None`` for ``FAILED`` and ``SKIPPED``
        outcomes because the cache is not touched in those branches.

        Outcome mapping:

        - ``FAILED`` — network exception, timeout, HTTP 5xx, malformed
          payload, auth failure (401/403), quota (402/429). The cache
          is **not** mutated; the ping row records the failure for
          observability.
        - ``SUCCESS_EMPTY`` — HTTP 2xx with a model list that did not
          survive normalization (zero models after filtering). The
          cache learns the provider returned nothing this cycle but
          prior support is preserved.
        - ``SUCCESS_PARTIAL`` — HTTP 2xx with a non-empty list that
          did not fully resolve (e.g. mixed resolved/unresolved
          protocols). Prior support is preserved; new models are added
          but the destructive path is disabled for that cycle.
        - ``SUCCESS_AUTHORITATIVE`` — HTTP 2xx with a fully
          protocol-resolved, non-empty list. The cache update honors
          the configured ``catalog_withdrawal_policy``: under the
          default ``preserve_until_health`` policy, the call is
          non-destructive; ``confirmed_once`` / ``confirmed_twice``
          enable withdrawal.
        - ``SKIPPED`` — the fetcher returned without contacting
          upstream (e.g. ``models_endpoint = DISABLED``). The cache is
          not touched.
        """
        try:
            result = await fetch_models_for_account(
                client,
                api_key,
                account_name,
                models_method=models_method,
                models_path=models_path,
                provider_cfg=provider_cfg,
            )

            # Record ping data for this provider/account
            if self._ping_repo is not None:
                async with self._db.transaction():
                    await self._ping_repo.record_ping(
                        provider_id=provider_id,
                        account_name=account_name,
                        latency_ms=result.latency_ms,
                        status_code=result.status_code,
                        error=result.error,
                        model_count=result.model_count,
                    )

            # Failed/empty response: preserve prior support, do not
            # touch the destructive cache path.
            if not result.response:
                # If the fetcher flagged a specific error, this is a
                # FAILED refresh (network/5xx/auth/quota/malformed
                # payload). Otherwise (no error, just empty data) it
                # is SUCCESS_EMPTY.
                if result.error is not None or result.status_code is None:
                    logger.warning(
                        "Catalog refresh for account %r on provider %r "
                        "preserved prior support after failure: %s "
                        "(status=%s, models_seen=%d)",
                        account_name,
                        provider_id,
                        result.error or "no response",
                        result.status_code,
                        result.model_count,
                    )
                    return AccountCatalogOutcome.FAILED, None
                logger.info(
                    "Catalog refresh for account %r on provider %r returned "
                    "an empty model list; prior support preserved",
                    account_name,
                    provider_id,
                )
                # Still record the empty response in the cache so the
                # support set reflects what the upstream is currently
                # advertising. The non-destructive default keeps prior
                # rows alive.
                update = self._cache.update_from_account(account_name, provider_id, [])
                return AccountCatalogOutcome.SUCCESS_EMPTY, update

            models = normalize_models(result.response)

            # An HTTP 2xx with a non-empty ``data`` list that survives
            # normalization as zero models is treated as an empty
            # refresh (e.g. an Anthropic-shaped response whose items
            # failed validation). The cache is preserved non-destructively
            # and the cycle's outcome counter reports ``SUCCESS_EMPTY``.
            if not models and not result.error:
                logger.info(
                    "Catalog refresh for account %r on provider %r returned "
                    "no normalizable models; prior support preserved",
                    account_name,
                    provider_id,
                )
                update = self._cache.update_from_account(account_name, provider_id, [])
                return AccountCatalogOutcome.SUCCESS_EMPTY, update
            provider_cfg = provider_cfg or self._config.providers.get(provider_id)

            # Apply per-model protocol resolution (Section 11)
            for model in models:
                # Provider-specific config takes precedence over a global
                # override, matching limit and pricing resolution.
                provider_override = (
                    provider_cfg.model_overrides.get(model["model_id"])
                    if provider_cfg is not None
                    else None
                )
                global_override = self._config.model_overrides.get(model["model_id"])
                protocol_override = (
                    provider_override.protocol
                    if provider_override is not None
                    and provider_override.protocol is not None
                    else (
                        global_override.protocol
                        if global_override is not None
                        else None
                    )
                )
                if protocol_override is not None:
                    model["protocol"] = protocol_override
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
                        persisted = self._cache.get_provider_model_entry(
                            model["model_id"], provider_id
                        )
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

            # Resolve effective limits for each model
            for model in models:
                capabilities = model.get("capabilities", {})
                source_metadata = model.get("source_metadata", {})
                effective = self._limit_resolver.resolve(
                    provider_id=provider_id,
                    model_id=model["model_id"],
                    capabilities=capabilities,
                    source_metadata=source_metadata,
                )
                # Discover upstream limits separately for diagnostics
                disc_ctx, disc_inp, disc_out = extract_upstream_limits(
                    capabilities, source_metadata
                )
                model["discovered_limits"] = {
                    "context_tokens": disc_ctx,
                    "input_tokens": disc_inp,
                    "output_tokens": disc_out,
                }
                model["effective_limits"] = effective.as_dict()

            # Decide authoritative / destructive flags from the
            # configured withdrawal policy. A "partial" outcome is
            # triggered when at least one model in the response has
            # an unresolved protocol that the provider-side override
            # could not pin: those rows are added to the cache but
            # we do not trust the response to be a complete
            # withdrawal confirmation, so we mark the outcome
            # SUCCESS_PARTIAL and never allow withdrawals for it.
            authoritative, allow_withdrawals, outcome = self._classify_authoritative(
                models=models,
                provider_cfg=provider_cfg,
            )
            update = self._cache.update_from_account(
                account_name,
                provider_id,
                models,
                authoritative=authoritative,
                allow_withdrawals=allow_withdrawals,
            )
            if outcome is AccountCatalogOutcome.SUCCESS_PARTIAL:
                logger.warning(
                    "Catalog refresh for account %r on provider %r returned a "
                    "partially-normalized model list; prior support preserved, "
                    "withdrawals disabled for this cycle",
                    account_name,
                    provider_id,
                )
            logger.debug(
                "Account %r: %s found %d models (added=%d, updated=%d, "
                "preserved=%d, withdrawn=%d)",
                account_name,
                outcome.value,
                len(models),
                update.added_support,
                update.updated_support,
                update.preserved_support,
                update.withdrawn_support,
            )
            return outcome, update
        except Exception as exc:
            logger.warning(
                "Catalog refresh for account %r on provider %r preserved prior "
                "support after exception: %s",
                account_name,
                provider_id,
                exc,
            )
            return AccountCatalogOutcome.FAILED, None

    def _classify_authoritative(
        self,
        *,
        models: list[dict[str, Any]],
        provider_cfg: ProviderConfig | None,
    ) -> tuple[bool, bool, AccountCatalogOutcome]:
        """Decide whether a successful refresh may destructively update.

        The withdrawal policy on ``ModelsConfig.catalog_withdrawal_policy``
        controls when ``allow_withdrawals`` is set. A response is
        treated as ``SUCCESS_PARTIAL`` when at least one model in the
        response is missing a resolved protocol; the cache update is
        still applied (so newly-discovered models land) but the
        withdrawal flag is forced off for that cycle because the
        response is not a complete model list.
        """
        policy = self._config.models.catalog_withdrawal_policy
        allow_withdrawals = policy != "preserve_until_health"
        has_unresolved = any(not model.get("protocol") for model in models)
        if has_unresolved and not allow_withdrawals:
            return True, False, AccountCatalogOutcome.SUCCESS_PARTIAL
        if has_unresolved:
            return True, False, AccountCatalogOutcome.SUCCESS_PARTIAL
        return True, allow_withdrawals, AccountCatalogOutcome.SUCCESS_AUTHORITATIVE

    async def _load_cached_models(self) -> None:
        """Load previously cached models from the database."""
        try:
            rows = await self._db.fetch_all(
                "SELECT model_id, display_name, protocol, "
                "capabilities, source_metadata, "
                "first_seen_at, last_seen_at, protocol_source FROM models "
                "WHERE model_id <> ?",
                (DEPRECATED_MODEL_ID,),
            )
            for row in rows:
                model_id = str(row["model_id"])
                caps = _parse_metadata_object(
                    row["capabilities"],
                    model_id=model_id,
                    field_name="capabilities",
                )
                meta = _parse_metadata_object(
                    row["source_metadata"],
                    model_id=model_id,
                    field_name="source_metadata",
                )
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

            provider_rows = await self._db.fetch_all(
                "SELECT model_id, provider_id, display_name, protocol, "
                "capabilities, source_metadata, protocol_source, "
                "first_seen_at, last_seen_at "
                "FROM provider_model_metadata "
                "WHERE model_id <> ?",
                (DEPRECATED_MODEL_ID,),
            )
            for row in provider_rows:
                provider_id = str(row["provider_id"])
                model_id = str(row["model_id"])
                caps = _parse_metadata_object(
                    row["capabilities"],
                    model_id=model_id,
                    field_name="capabilities",
                )
                meta = _parse_metadata_object(
                    row["source_metadata"],
                    model_id=model_id,
                    field_name="source_metadata",
                )
                effective = self._limit_resolver.resolve(
                    provider_id=provider_id,
                    model_id=model_id,
                    capabilities=caps,
                    source_metadata=meta,
                )
                disc_ctx, disc_inp, disc_out = extract_upstream_limits(caps, meta)
                self._cache.set_provider_model_entry(
                    model_id,
                    provider_id,
                    {
                        "model_id": model_id,
                        "display_name": row["display_name"],
                        "protocol": row["protocol"],
                        "protocol_source": row["protocol_source"],
                        "capabilities": caps,
                        "source_metadata": meta,
                        "first_seen_at": _ts_to_unix(row["first_seen_at"]),
                        "last_seen_at": _ts_to_unix(row["last_seen_at"]),
                        "discovered_limits": {
                            "context_tokens": disc_ctx,
                            "input_tokens": disc_inp,
                            "output_tokens": disc_out,
                        },
                        "effective_limits": effective.as_dict(),
                    },
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
                provider_id = (
                    id_to_provider.get(row["account_id"]) or DEFAULT_PROVIDER_ID
                )
                if account_name:
                    self._cache.set_account_provider(account_name, provider_id)
                    if self._cache.has_model(model_id):
                        self._cache.add_account_support(model_id, account_name)
                        # Populate per-provider metadata from global entry
                        # so provider-suffixed exposure works before the
                        # next live refresh.
                        if (
                            self._cache.get_provider_model_entry(model_id, provider_id)
                            is None
                        ):
                            model_info = self._cache.get_model(model_id)
                            if model_info is not None:
                                provider_entry = dict(model_info)
                                # Rerun limit resolution so config changes
                                # take effect immediately after restart
                                # without waiting for a live refresh.
                                caps = provider_entry.get("capabilities", {})
                                meta = provider_entry.get("source_metadata", {})
                                effective = self._limit_resolver.resolve(
                                    provider_id=provider_id,
                                    model_id=model_id,
                                    capabilities=caps,
                                    source_metadata=meta,
                                )
                                disc_ctx, disc_inp, disc_out = extract_upstream_limits(
                                    caps, meta
                                )
                                provider_entry["discovered_limits"] = {
                                    "context_tokens": disc_ctx,
                                    "input_tokens": disc_inp,
                                    "output_tokens": disc_out,
                                }
                                provider_entry["effective_limits"] = effective.as_dict()
                                self._cache.set_provider_model_entry(
                                    model_id, provider_id, provider_entry
                                )

            if self._cache.model_count > 0:
                logger.info(
                    "Loaded %d cached models from database",
                    self._cache.model_count,
                )
            self._cache.hydrate_account_refresh_ages()
            self._cache.hydrate_refresh_age()
            self._cache_loaded = True
        except Exception:
            logger.exception("Failed to load cached models")
            raise

    async def _persist_catalog(self) -> None:
        """Persist the in-memory catalog to the database."""
        now = _dt.datetime.now(_dt.UTC).timestamp()
        now_iso = _unix_to_db_timestamp(now, fallback=now)

        acct_rows = await self._db.fetch_all("SELECT id, name FROM accounts")
        existing_support_rows = await self._db.fetch_all(
            "SELECT account_id, model_id FROM account_models WHERE enabled = 1"
        )
        latest_prices = await PriceSnapshotRepository(self._db).get_all_latest()
        model_rows: list[tuple[Any, ...]] = []
        provider_model_rows: list[tuple[Any, ...]] = []
        desired_support: set[tuple[int, str]] = set()

        for model_id, model_info in self._cache.get_all_models().items():
            if model_id == DEPRECATED_MODEL_ID:
                # The placeholder only exists in the durable layer so
                # historical request rows can keep their FK after the
                # upstream model is deleted. Never treat it as a live
                # catalog entry.
                continue
            protocol = model_info.get("protocol")
            if protocol not in SUPPORTED_PROTOCOLS:
                if model_id in self._warned_unresolved_models:
                    logger.debug(
                        "Skipping unresolved model during catalog persistence: %s",
                        model_id,
                    )
                else:
                    logger.warning(
                        "Skipping unresolved model during catalog persistence: %s",
                        model_id,
                    )
                    self._warned_unresolved_models.add(model_id)
                continue

            protocol_source = model_info.get("protocol_source")
            if protocol_source == "unresolved":
                protocol_source = None
            model_rows.append(
                (
                    model_id,
                    model_info.get("display_name"),
                    model_info["protocol"],
                    json.dumps(model_info.get("capabilities", {})),
                    json.dumps(model_info.get("source_metadata", {})),
                    _unix_to_db_timestamp(
                        model_info.get("first_seen_at"), fallback=now
                    ),
                    _unix_to_db_timestamp(model_info.get("last_seen_at"), fallback=now),
                    protocol_source,
                )
            )

            supporting_accounts = self._cache.get_supporting_accounts_for_model(
                model_id
            )
            desired_support.update(
                (int(acct_row["id"]), model_id)
                for acct_row in acct_rows
                if acct_row["name"] in supporting_accounts
            )

        persisted_model_ids = {str(row[0]) for row in model_rows}
        for (
            model_id,
            provider_id,
        ), model_info in self._cache.get_provider_model_entries().items():
            if model_id not in persisted_model_ids:
                continue
            provider_model_rows.append(
                (
                    model_id,
                    provider_id,
                    model_info.get("display_name"),
                    model_info.get("protocol"),
                    json.dumps(model_info.get("capabilities", {})),
                    json.dumps(model_info.get("source_metadata", {})),
                    model_info.get("protocol_source"),
                    _unix_to_db_timestamp(
                        model_info.get("first_seen_at"), fallback=now
                    ),
                    _unix_to_db_timestamp(model_info.get("last_seen_at"), fallback=now),
                    "resolved" if model_info.get("protocol") else "unresolved",
                )
            )

        existing_support = {
            (int(row["account_id"]), str(row["model_id"]))
            for row in existing_support_rows
        }
        support_to_enable = desired_support - existing_support
        support_to_disable = existing_support - desired_support

        async with self._db.transaction():
            await self._db.execute_many(
                """
                    INSERT INTO models (
                        model_id, display_name, protocol,
                        capabilities, source_metadata,
                        first_seen_at, last_seen_at, protocol_source,
                        resolution_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'resolved')
                    ON CONFLICT(model_id) DO UPDATE SET
                        display_name = excluded.display_name,
                        protocol = excluded.protocol,
                        capabilities = excluded.capabilities,
                        source_metadata = excluded.source_metadata,
                        last_seen_at = excluded.last_seen_at,
                        protocol_source = excluded.protocol_source,
                        resolution_status = 'resolved'
                """,
                model_rows,
            )
            await self._db.execute_many(
                """
                    INSERT INTO provider_model_metadata (
                        model_id, provider_id, display_name, protocol,
                        capabilities, source_metadata, protocol_source,
                        first_seen_at, last_seen_at, resolution_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(model_id, provider_id) DO UPDATE SET
                        display_name = excluded.display_name,
                        protocol = excluded.protocol,
                        capabilities = excluded.capabilities,
                        source_metadata = excluded.source_metadata,
                        protocol_source = excluded.protocol_source,
                        last_seen_at = excluded.last_seen_at,
                        resolution_status = excluded.resolution_status
                """,
                provider_model_rows,
            )
            await self._db.execute_many(
                """
                    INSERT INTO account_models (
                        account_id, model_id, enabled, created_at
                    ) VALUES (?, ?, 1, ?)
                    ON CONFLICT(account_id, model_id) DO UPDATE SET enabled = 1
                """,
                [(*pair, now_iso) for pair in sorted(support_to_enable)],
            )
            await self._db.execute_many(
                "UPDATE account_models SET enabled = 0 "
                "WHERE account_id = ? AND model_id = ? AND enabled = 1",
                sorted(support_to_disable),
            )

            # Persist provider-specific pricing only. A global snapshot would
            # create phantom default-provider pricing when a model is offered
            # exclusively by another provider.
            for (
                pid_model_id,
                pid,
            ), pinfo in self._cache.get_provider_model_entries().items():
                if self._cache.has_model(pid_model_id):
                    await self._maybe_insert_price_snapshot(
                        pid_model_id,
                        pinfo,
                        provider_id=pid,
                        latest=latest_prices.get((pid_model_id, pid)),
                    )

            # Reconcile the durable catalog with the live cache.
            # Models and provider rows that are no longer advertised
            # by any account are deleted; rows with historical
            # request/reservation references are relinked to the
            # placeholder so usage data is preserved.
            await self._reconcile_catalog(persisted_model_ids)

    async def _reconcile_catalog(self, live_model_ids: set[str]) -> None:
        """Align the durable catalog tables with the live in-memory cache.

        The caller's transaction is already open. Steps:

        1. Ensure the placeholder ``models`` row exists.
        2. Relink any durable model row that is not in the live
           cache and that has historical ``requests`` or
           ``reservations`` references. The original id is preserved
           in ``original_model_id``; the FK pointer moves to
           ``__deprecated__``.
        3. Delete the (now-referenced-free) original ``models`` rows
           and any ``provider_model_metadata`` rows that the live
           cache no longer carries.
        4. Delete ``account_models`` rows that are currently
           disabled and have no recent ``requests`` activity so old
           withdraw-enable toggles do not accumulate.

        Errors during relink leave the durable state unchanged:
        SQLite is the only place these rows are mutated, so the
        catch-and-log branch is informational only.
        """
        reconciliation = CatalogReconciliationRepository(self._db)
        await reconciliation.ensure_placeholder()

        # Step 1: collect durable model ids that the live cache no
        # longer carries. The placeholder must be excluded so it is
        # never a candidate for relink or deletion.
        durable_rows = await self._db.fetch_all(
            "SELECT model_id FROM models WHERE model_id <> ?",
            (DEPRECATED_MODEL_ID,),
        )
        durable_model_ids = {str(row["model_id"]) for row in durable_rows}
        withdrawn_ids = durable_model_ids - live_model_ids

        if not withdrawn_ids and not await self._has_orphan_provider_rows(
            live_model_ids
        ):
            return

        relinked_total = 0
        deleted_models = 0
        for withdrawn_id in sorted(withdrawn_ids):
            try:
                counts = await reconciliation.relink_model(withdrawn_id)
            except Exception:
                logger.exception(
                    "Failed to relink withdrawn model %r; leaving row in place",
                    withdrawn_id,
                )
                continue
            if counts["requests"] or counts["reservations"]:
                relinked_total += counts["requests"] + counts["reservations"]
                logger.info(
                    "Relinked withdrawn model %r: %d request(s), "
                    "%d reservation(s) moved to %r",
                    withdrawn_id,
                    counts["requests"],
                    counts["reservations"],
                    DEPRECATED_MODEL_ID,
                )
            await self._db.execute_write(
                "DELETE FROM account_models WHERE model_id = ?",
                (withdrawn_id,),
            )
            await self._db.execute_write(
                "DELETE FROM provider_model_metadata WHERE model_id = ?",
                (withdrawn_id,),
            )
            await self._db.execute_write(
                "DELETE FROM models WHERE model_id = ?",
                (withdrawn_id,),
            )
            deleted_models += 1

        deleted_provider_rows = await self._delete_orphan_provider_rows(live_model_ids)
        deleted_account_links = await self._delete_stale_account_links()

        if deleted_models or deleted_provider_rows or deleted_account_links:
            logger.info(
                "Catalog reconciliation: relinked=%d rows, "
                "deleted_models=%d, deleted_provider_rows=%d, "
                "deleted_account_links=%d",
                relinked_total,
                deleted_models,
                deleted_provider_rows,
                deleted_account_links,
            )

    async def _has_orphan_provider_rows(self, live_model_ids: set[str]) -> bool:
        """Return whether durable provider rows reference a withdrawn model."""
        rows = await self._db.fetch_all(
            "SELECT model_id FROM provider_model_metadata WHERE model_id <> ?",
            (DEPRECATED_MODEL_ID,),
        )
        durable = {str(r["model_id"]) for r in rows}
        return bool(durable - live_model_ids)

    async def _delete_orphan_provider_rows(self, live_model_ids: set[str]) -> int:
        """Delete ``provider_model_metadata`` rows the live cache no longer carries."""
        rows = await self._db.fetch_all(
            "SELECT model_id, provider_id FROM provider_model_metadata "
            "WHERE model_id <> ?",
            (DEPRECATED_MODEL_ID,),
        )
        stale: list[tuple[Any, ...]] = []
        for row in rows:
            model_id = str(row["model_id"])
            provider_id = str(row["provider_id"])
            if model_id not in live_model_ids:
                stale.append((model_id, provider_id))
        if not stale:
            return 0
        await self._db.execute_many(
            "DELETE FROM provider_model_metadata "
            "WHERE model_id = ? AND provider_id = ?",
            stale,
        )
        return len(stale)

    async def _delete_stale_account_links(self) -> int:
        """Delete ``account_models`` rows that are disabled and unused.

        A row counts as unused when there are no ``requests`` rows
        referencing the (account_id, model_id) pair within the
        retention window.  Historical request activity still wins
        because the durable ``requests`` table is the source of
        truth for the dashboard.
        """
        rows = await self._db.fetch_all(
            "SELECT am.account_id, am.model_id FROM account_models am "
            "LEFT JOIN requests r ON r.account_id = am.account_id "
            "  AND r.model_id = am.model_id "
            "WHERE am.enabled = 0 AND r.id IS NULL"
        )
        if not rows:
            return 0
        targets = [(int(r["account_id"]), str(r["model_id"])) for r in rows]
        await self._db.execute_many(
            "DELETE FROM account_models WHERE account_id = ? AND model_id = ?",
            targets,
        )
        return len(targets)

    async def _maybe_insert_price_snapshot(
        self,
        model_id: str,
        model_info: dict[str, Any],
        provider_id: str = DEFAULT_PROVIDER_ID,
        latest: dict[str, Any] | None = None,
    ) -> None:
        """Insert a price snapshot if pricing data is available.

        Each pricing category (input, output, cache-read, cache-write)
        is resolved independently through ``pricing_resolver``: a TOML
        override is authoritative for any category it sets, and any
        category not set by TOML falls back to upstream metadata. The
        resulting ``source`` is

        - ``"config"`` if every present value came from TOML,
        - ``"upstream"`` if every present value came from upstream
          metadata,
        - ``"mixed"`` if the present values came from a mix of both.

        The snapshot is only inserted when its values differ from the
        latest snapshot for the same model.
        """
        # Per-category override values (None means: no override for this
        # category, fall back to upstream).
        global_override = self._config.model_overrides.get(model_id)
        provider_cfg = self._config.providers.get(provider_id)
        provider_override = (
            provider_cfg.model_overrides.get(model_id)
            if provider_cfg is not None
            else None
        )
        override_values: dict[str, Any] = {}
        # Populate global values first, then replace individual categories
        # supplied by the provider-specific override.
        for override in (global_override, provider_override):
            if override is None:
                continue
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

        resolved = resolve_pricing_from_metadata(
            model_id=model_id,
            provider_id=provider_id,
            model_info=model_info,
            override_values=override_values,
        )
        if resolved is None and self._catalog_pipeline is not None:
            # External catalogs (OpenRouter, OpenCode Zen) supply
            # authoritative pricing for models whose upstream /models
            # endpoint does not expose pricing metadata. Falls through
            # to the existing ``return`` below if no catalog matches.
            resolved = await self._catalog_pipeline.resolve(
                provider_id=provider_id,
                model_id=model_id,
            )
        if resolved is None:
            return

        input_price = resolved.input_price_per_1k
        output_price = resolved.output_price_per_1k
        cache_read_price = resolved.cache_read_per_million_microdollars
        cache_write_price = resolved.cache_write_per_million_microdollars
        source = resolved.source

        # Skip insert when every field already matches the latest snapshot.
        snapshot_repo = PriceSnapshotRepository(self._db)
        if latest is not None:
            old_input = latest.get("input_price_per_1k")
            old_output = latest.get("output_price_per_1k")
            old_cache_read = latest.get("cache_read_per_million_microdollars")
            old_cache_write = latest.get("cache_write_per_million_microdollars")
            old_source = latest.get("source")
            old_provider = latest.get("provider_id", DEFAULT_PROVIDER_ID)
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
            source_detail=resolved.source_detail,
            source_confidence=resolved.source_confidence,
            catalog_source=resolved.source_provider_id,
        )
        if self._price_change_callback is not None:
            self._price_change_callback(model_id, provider_id)
        logger.debug(
            "Inserted price snapshot for %s: input=%s output=%s "
            "cache_read=%s cache_write=%s source=%s detail=%s confidence=%s",
            model_id,
            input_price,
            output_price,
            cache_read_price,
            cache_write_price,
            source,
            resolved.source_detail,
            resolved.source_confidence,
        )

    def get_models_for_exposure(
        self,
        health_manager: HealthManager | None = None,
    ) -> list[dict[str, Any]]:
        """Get models to expose via /v1/models.

        When ``models.collapse_models`` is true, base model IDs are exposed
        unsuffixed with conservative-merged limits across providers. When
        false (the default), each ``(model_id, provider_id)`` pair is
        exposed as a suffixed ``model-id/provider-id`` entry. ``expose_mode``
        is applied in both branches.
        """
        eligible = {s.name for s in self._registry.get_eligible_states()}
        if self._config.models.expose_mode == "healthy_union" and health_manager:
            eligible = {
                name for name in eligible if health_manager.is_account_healthy(name)
            }
        if self._config.models.collapse_models:
            return self._cache.get_models_for_exposure(
                self._config.models.expose_mode,
                eligible,
            )
        return self._cache.get_provider_suffixed_models(
            self._config.models.expose_mode,
            eligible,
        )

    def get_models_for_dispatch(
        self,
    ) -> list[dict[str, Any]]:
        """Return provider-suffixed model entries for internal dispatch.

        Independent of ``models.collapse_models`` — the runtime always
        routes per-suffixed-ID. OpenCode provider-suffixed clients also
        consume this shape directly.
        """
        eligible = {s.name for s in self._registry.get_eligible_states()}
        return self._cache.get_provider_suffixed_models(
            self._config.models.expose_mode,
            eligible,
        )

    def is_model_available(self, model_id: str) -> bool:
        """Check if a model is available from any eligible account."""
        eligible = {s.name for s in self._registry.get_eligible_states()}
        return self._cache.is_model_available(model_id, eligible)
