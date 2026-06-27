"""Deterministic alias registry for external pricing catalog lookups.

This module answers the question: *given a provider-native model ID,
which external catalog entry should we fetch pricing from?*

Key invariant: the resolver never falls back to substring or edit-
distance matching. If the lookup is ambiguous (e.g. MiMo 2.5 vs
MiMo 2.5 Pro both candidates for ``mimo``), it returns ``None`` and
the higher-level catalog layer logs a warning. Operators must declare
each safe alias explicitly via the ``model_pricing_aliases`` table.

The in-memory cache mirrors the table so the hot path is a dict
lookup rather than a SQLite query.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from eggpool.constants import DEFAULT_PROVIDER_ID

if TYPE_CHECKING:
    from eggpool.db.connection import Database

logger = logging.getLogger(__name__)


# Confidence values stored on the alias row.
ALIAS_CONFIDENCE_EXACT = "exact"
ALIAS_CONFIDENCE_CURATED_ALIAS = "curated_alias"
ALIAS_CONFIDENCE_AMBIGUOUS_SKIP = "ambiguous_skip"

# Confidence values used by the resolver when the lookup actually
# produced a usable catalog entry. ``ambiguous_skip`` is never one of
# these — it is filtered out before the catalog call.
ALIAS_RESOLVABLE_CONFIDENCES = frozenset(
    {ALIAS_CONFIDENCE_EXACT, ALIAS_CONFIDENCE_CURATED_ALIAS}
)


@dataclass(frozen=True)
class PricingAlias:
    """One row of the alias registry."""

    provider_id: str
    upstream_model_id: str
    catalog_source: str
    catalog_model_id: str
    confidence: str
    notes: str | None = None


@dataclass(frozen=True)
class AliasLookupResult:
    """Result of an alias lookup.

    ``resolved`` is the alias row the resolver will use to fetch a
    price; ``None`` means no usable alias exists. ``ambiguous`` carries
    any non-resolvable rows (e.g. ``ambiguous_skip``) so the caller
    can log a diagnostic warning without forcing the resolver to act
    on them.
    """

    resolved: PricingAlias | None
    ambiguous: tuple[PricingAlias, ...] = ()


class PricingAliasResolver:
    """Resolve (provider_id, upstream_model_id, catalog_source) → alias.

    The resolver loads all rows from ``model_pricing_aliases`` once and
    indexes them in memory by ``(provider_id, upstream_model_id,
    catalog_source)`` and ``(provider_id, upstream_model_id)`` for
    ambiguity detection. Call ``refresh`` after writes to the table.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._by_key: dict[tuple[str, str, str], PricingAlias] = {}
        self._by_pair: dict[tuple[str, str], list[PricingAlias]] = {}
        self._loaded = False

    async def refresh(self) -> None:
        """Reload the alias cache from the database."""
        rows = await self._db.fetch_all(
            """
            SELECT provider_id, upstream_model_id, catalog_source,
                   catalog_model_id, confidence, notes
            FROM model_pricing_aliases
            """
        )
        by_key: dict[tuple[str, str, str], PricingAlias] = {}
        by_pair: dict[tuple[str, str], list[PricingAlias]] = {}
        for row in rows:
            alias = PricingAlias(
                provider_id=row["provider_id"] or DEFAULT_PROVIDER_ID,
                upstream_model_id=row["upstream_model_id"],
                catalog_source=row["catalog_source"],
                catalog_model_id=row["catalog_model_id"],
                confidence=row["confidence"],
                notes=row["notes"],
            )
            key = (
                alias.provider_id,
                alias.upstream_model_id,
                alias.catalog_source,
            )
            by_key[key] = alias
            by_pair.setdefault((alias.provider_id, alias.upstream_model_id), []).append(
                alias
            )
        self._by_key = by_key
        self._by_pair = by_pair
        self._loaded = True

    async def _ensure_loaded(self) -> None:
        if not self._loaded:
            await self.refresh()

    async def lookup(
        self,
        *,
        provider_id: str,
        upstream_model_id: str,
        catalog_source: str,
    ) -> AliasLookupResult:
        """Return the alias row for one (provider, model, catalog)."""
        await self._ensure_loaded()
        pair = (provider_id, upstream_model_id)
        rows = self._by_pair.get(pair, ())
        key = (provider_id, upstream_model_id, catalog_source)
        match = self._by_key.get(key)
        ambiguous_rows = tuple(
            row for row in rows if row.confidence == ALIAS_CONFIDENCE_AMBIGUOUS_SKIP
        )
        if match is not None and match.confidence in ALIAS_RESOLVABLE_CONFIDENCES:
            return AliasLookupResult(resolved=match, ambiguous=ambiguous_rows)
        if match is not None and match.confidence == ALIAS_CONFIDENCE_AMBIGUOUS_SKIP:
            return AliasLookupResult(resolved=None, ambiguous=ambiguous_rows)
        return AliasLookupResult(resolved=None, ambiguous=ambiguous_rows)

    async def list_aliases_for_pair(
        self, *, provider_id: str, upstream_model_id: str
    ) -> list[PricingAlias]:
        await self._ensure_loaded()
        return list(self._by_pair.get((provider_id, upstream_model_id), ()))

    @staticmethod
    def detect_ambiguous_candidates(
        rows: list[PricingAlias],
        *,
        catalog_source: str,
    ) -> bool:
        """Return True if multiple distinct catalog candidates exist.

        Used by the catalog resolver layer to decide whether it is safe
        to fall back to substring/prefix matching. With the strict
        alias-only policy this always returns ``False``; the helper is
        kept so future catalog logic can opt into looser matching
        without re-introducing ambiguity at the alias layer.
        """
        distinct = {
            row.catalog_model_id
            for row in rows
            if row.catalog_source == catalog_source
            and row.confidence in ALIAS_RESOLVABLE_CONFIDENCES
        }
        return len(distinct) > 1


async def seed_default_aliases(db: Database) -> int:
    """Seed the alias table with a curated starter set.

    Idempotent: re-running on an already-populated table is a no-op.
    Returns the number of new rows inserted (0 if every row already
    existed).
    """
    defaults: list[dict[str, Any]] = [
        # Exact ID matches
        {
            "provider_id": "opencode-go",
            "upstream_model_id": "xiaomi/mimo-v2.5",
            "catalog_source": "openrouter",
            "catalog_model_id": "xiaomi/mimo-v2.5",
            "confidence": ALIAS_CONFIDENCE_EXACT,
            "notes": "Provider exposes the OpenRouter-canonical ID.",
        },
        {
            "provider_id": "opencode-go",
            "upstream_model_id": "xiaomi/mimo-v2.5-pro",
            "catalog_source": "openrouter",
            "catalog_model_id": "xiaomi/mimo-v2.5-pro",
            "confidence": ALIAS_CONFIDENCE_EXACT,
            "notes": "Provider exposes the OpenRouter-canonical ID.",
        },
        # Curated aliases (provider-native ID differs from catalog ID)
        {
            "provider_id": "opencode-go",
            "upstream_model_id": "mimo-v2.5",
            "catalog_source": "openrouter",
            "catalog_model_id": "xiaomi/mimo-v2.5",
            "confidence": ALIAS_CONFIDENCE_CURATED_ALIAS,
            "notes": "Provider strips the vendor/ prefix; map to OpenRouter.",
        },
        {
            "provider_id": "opencode-go",
            "upstream_model_id": "mimo-v2.5-pro",
            "catalog_source": "openrouter",
            "catalog_model_id": "xiaomi/mimo-v2.5-pro",
            "confidence": ALIAS_CONFIDENCE_CURATED_ALIAS,
            "notes": "Provider strips the vendor/ prefix; map to OpenRouter.",
        },
    ]
    inserted = 0
    async with db.transaction():
        for row in defaults:
            existing = await db.fetch_one(
                """
                SELECT 1 FROM model_pricing_aliases
                WHERE provider_id = ? AND upstream_model_id = ?
                  AND catalog_source = ?
                """,
                (row["provider_id"], row["upstream_model_id"], row["catalog_source"]),
            )
            if existing is not None:
                continue
            await db.execute_write(
                """
                INSERT INTO model_pricing_aliases
                    (provider_id, upstream_model_id, catalog_source,
                     catalog_model_id, confidence, notes)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    row["provider_id"],
                    row["upstream_model_id"],
                    row["catalog_source"],
                    row["catalog_model_id"],
                    row["confidence"],
                    row["notes"],
                ),
            )
            inserted += 1
    if inserted:
        logger.info("Seeded %d pricing-alias rows", inserted)
    return inserted
