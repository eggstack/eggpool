"""External catalog pricing resolver interface and OpenRouter implementation.

Catalog resolvers answer the question: *what does this external catalog
say about pricing for a given model ID?* Each catalog is its own
implementation behind the ``PricingCatalogResolver`` Protocol; the
top-level ``CatalogResolverPipeline`` consults them in priority order
and caches responses in memory with a TTL.

Refusal semantics:

- The resolver never falls back to substring or edit-distance matching
  when multiple catalog candidates could fit the queried ID.
- The pipeline calls ``PricingAliasResolver.lookup()`` first; only
  aliases the operator explicitly declared are ever consulted.
- If a catalog returns no match for the alias-derived ID, the pipeline
  emits a warning and returns ``None`` rather than guessing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast

import httpx

from eggpool.catalog.pricing_resolver import (
    CONFIDENCE_CURATED_ALIAS,
    CONFIDENCE_EXACT_EXTERNAL_ID,
    SOURCE_DETAIL_OPENROUTER,
    ResolvedPricing,
)

if TYPE_CHECKING:
    from eggpool.catalog.pricing_aliases import PricingAlias, PricingAliasResolver

logger = logging.getLogger(__name__)


# Default TTL when none is configured for a catalog. 24 hours matches
# the OpenCode Zen / OpenRouter example in the plan.
DEFAULT_CATALOG_TTL_SECONDS = 86_400


@dataclass
class CatalogConfig:
    """Configuration for one external pricing catalog."""

    name: str
    enabled: bool = True
    priority: int = 100
    ttl_seconds: int = DEFAULT_CATALOG_TTL_SECONDS
    base_url: str | None = None
    api_key: str | None = None
    options: dict[str, object] = field(default_factory=dict[str, object])


@dataclass
class CatalogEntry:
    """One model entry returned by an external catalog."""

    catalog_model_id: str
    input_price_per_1k: float | None = None
    output_price_per_1k: float | None = None
    cache_read_per_million_microdollars: int | None = None
    cache_write_per_million_microdollars: int | None = None
    raw: dict[str, object] = field(default_factory=dict[str, object])


class PricingCatalogResolver(Protocol):
    """Interface every concrete catalog resolver must satisfy."""

    name: str

    @property
    def priority(self) -> int:
        """Return lower-first ordering priority for this resolver."""
        ...

    async def fetch_catalog(self) -> dict[str, CatalogEntry]:
        """Fetch the full catalog and index entries by catalog model ID.

        Network failures should raise ``CatalogFetchError`` so the
        pipeline can record an audit event without taking the request
        path down.
        """
        ...

    def to_resolved_pricing(
        self,
        *,
        entry: CatalogEntry,
        provider_id: str,
        model_id: str,
        alias: PricingAlias,
    ) -> ResolvedPricing:
        """Convert a fetched catalog entry into a ResolvedPricing."""
        ...


class CatalogFetchError(RuntimeError):
    """Raised when a catalog fetch fails (network, parse, auth)."""


class CatalogHttpClient(Protocol):
    """Minimal async HTTP client interface used by catalog resolvers."""

    async def get(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        """Issue a GET request and return the HTTP response."""
        ...


class TTLCache:
    """Tiny TTL cache for catalog responses.

    The catalog fetch is a one-shot operation per TTL window; we do
    not need eviction beyond "expire at TTL". Re-fetching is cheap
    relative to the cost of holding tens of thousands of model rows
    in memory across restarts.
    """

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl = ttl_seconds
        self._data: dict[str, CatalogEntry] = {}
        self._fetched_at: float = 0.0
        self._lock: asyncio.Lock = asyncio.Lock()

    @property
    def age_seconds(self) -> float:
        if self._fetched_at == 0.0:
            return float("inf")
        return time.monotonic() - self._fetched_at

    @property
    def is_fresh(self) -> bool:
        return self.age_seconds < self._ttl

    @property
    def lock(self) -> asyncio.Lock:
        """Public accessor for the fetch lock.

        Exposed so callers can serialise double-checked fetches
        without reaching into a private attribute.
        """
        return self._lock

    def invalidate(self) -> None:
        self._data = {}
        self._fetched_at = 0.0

    def store(self, entries: dict[str, CatalogEntry]) -> None:
        self._data = dict(entries)
        self._fetched_at = time.monotonic()

    def get(self, key: str) -> CatalogEntry | None:
        return self._data.get(key)

    def snapshot(self) -> dict[str, CatalogEntry]:
        """Return a shallow copy of the cached entries."""
        return dict(self._data)


class OpenRouterCatalogResolver:
    """OpenRouter ``/models`` endpoint as an external pricing catalog."""

    name = "openrouter"

    def __init__(
        self,
        *,
        config: CatalogConfig,
        client: CatalogHttpClient,
        cache: TTLCache | None = None,
    ) -> None:
        self._config = config
        self._cache = cache or TTLCache(config.ttl_seconds)
        self._client = client

    @property
    def priority(self) -> int:
        return self._config.priority

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": "eggpool/1.0"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        return headers

    def _url(self, path: str) -> str:
        base_url = self._config.base_url or "https://openrouter.ai/api/v1"
        return f"{base_url.rstrip('/')}/{path.lstrip('/')}"

    async def fetch_catalog(self) -> dict[str, CatalogEntry]:
        if self._cache.is_fresh:
            return dict(self._cache.snapshot())
        async with self._cache.lock:
            if self._cache.is_fresh:
                return dict(self._cache.snapshot())
            try:
                response = await self._client.get(
                    self._url("/models"),
                    headers=self._headers(),
                )
                response.raise_for_status()
                # response.json() returns object; carry that through.
                payload_obj: object = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise CatalogFetchError(f"OpenRouter fetch failed: {exc}") from exc
            entries = self._parse_catalog(payload_obj)
            self._cache.store(entries)
            return entries

    @staticmethod
    def _parse_catalog(payload: object) -> dict[str, CatalogEntry]:
        entries: dict[str, CatalogEntry] = {}
        if not isinstance(payload, dict):
            return entries
        data_dict: dict[str, Any] = cast("dict[str, Any]", payload)
        data_obj: object = data_dict.get("data", [])
        if not isinstance(data_obj, list):
            return entries
        for raw_obj in cast("list[object]", data_obj):
            if not isinstance(raw_obj, dict):
                continue
            raw_dict: dict[str, Any] = cast("dict[str, Any]", raw_obj)
            model_id_obj: object = raw_dict.get("id")
            if not isinstance(model_id_obj, str) or not model_id_obj:
                continue
            pricing_obj: object = raw_dict.get("pricing") or {}
            entry = OpenRouterCatalogResolver._parse_entry(
                model_id_obj, pricing_obj, raw_dict
            )
            entries[model_id_obj] = entry
        return entries

    @staticmethod
    def _parse_entry(
        model_id: str, pricing: object, raw: dict[str, Any]
    ) -> CatalogEntry:
        """Translate an OpenRouter pricing block into a CatalogEntry."""
        from eggpool.catalog.pricing import (
            parse_microdollars_per_million,
            parse_price_per_1k,
        )

        def _opt(key: str) -> object | None:
            if not isinstance(pricing, dict):
                return None
            value: object = pricing.get(key)  # type: ignore[reportUnknownMemberType]
            if isinstance(value, (str, int, float)):
                return value
            return None

        # OpenRouter fields are dollars-per-token numeric strings.
        input_per_1k = parse_price_per_1k(_opt("prompt"), default_unit="token")
        output_per_1k = parse_price_per_1k(_opt("completion"), default_unit="token")
        cache_read = parse_microdollars_per_million(
            _opt("input_cache_read"), default_unit="token"
        )
        cache_write = parse_microdollars_per_million(
            _opt("input_cache_write"), default_unit="token"
        )
        return CatalogEntry(
            catalog_model_id=model_id,
            input_price_per_1k=input_per_1k,
            output_price_per_1k=output_per_1k,
            cache_read_per_million_microdollars=cache_read,
            cache_write_per_million_microdollars=cache_write,
            raw=raw,
        )

    def to_resolved_pricing(
        self,
        *,
        entry: CatalogEntry,
        provider_id: str,
        model_id: str,
        alias: PricingAlias,
    ) -> ResolvedPricing:
        confidence = (
            CONFIDENCE_EXACT_EXTERNAL_ID
            if alias.confidence == "exact"
            else CONFIDENCE_CURATED_ALIAS
        )
        return ResolvedPricing(
            input_price_per_1k=entry.input_price_per_1k,
            output_price_per_1k=entry.output_price_per_1k,
            cache_read_per_million_microdollars=entry.cache_read_per_million_microdollars,
            cache_write_per_million_microdollars=entry.cache_write_per_million_microdollars,
            source="upstream",
            source_detail=SOURCE_DETAIL_OPENROUTER,
            source_confidence=confidence,
            source_model_id=entry.catalog_model_id,
            source_provider_id=self.name,
        )


class CatalogResolverPipeline:
    """Run a list of catalog resolvers in priority order.

    The pipeline never auto-selects between candidates; it asks each
    alias first, then looks up the catalog entry by the exact catalog
    model ID the alias points to. If the alias is missing, the lookup
    is skipped entirely.
    """

    def __init__(
        self,
        *,
        resolvers: list[PricingCatalogResolver],
        alias_resolver: PricingAliasResolver,
    ) -> None:
        # Sort resolvers by configured priority ascending so operators
        # control which catalog wins when several can price the same model.
        self._resolvers = sorted(resolvers, key=lambda r: (r.priority, r.name))
        self._aliases = alias_resolver

    async def resolve(
        self,
        *,
        provider_id: str,
        model_id: str,
    ) -> ResolvedPricing | None:
        for resolver in self._resolvers:
            alias_result = await self._aliases.lookup(
                provider_id=provider_id,
                upstream_model_id=model_id,
                catalog_source=resolver.name,
            )
            if alias_result.resolved is None:
                if alias_result.ambiguous:
                    logger.warning(
                        "Catalog %s has ambiguous alias rows for %s/%s; "
                        "skipping external pricing",
                        resolver.name,
                        provider_id,
                        model_id,
                    )
                continue
            alias = alias_result.resolved
            try:
                catalog = await resolver.fetch_catalog()
            except CatalogFetchError as exc:
                logger.warning(
                    "Catalog %s fetch failed for %s/%s: %s",
                    resolver.name,
                    provider_id,
                    model_id,
                    exc,
                )
                continue
            entry = catalog.get(alias.catalog_model_id)
            if entry is None:
                logger.warning(
                    "Catalog %s has no entry for alias %s (upstream=%s/%s)",
                    resolver.name,
                    alias.catalog_model_id,
                    provider_id,
                    model_id,
                )
                continue
            return resolver.to_resolved_pricing(
                entry=entry,
                provider_id=provider_id,
                model_id=model_id,
                alias=alias,
            )
        return None
