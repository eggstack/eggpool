"""Price snapshot storage and derived cost calculation."""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from eggpool.constants import DEFAULT_PROVIDER_ID

if TYPE_CHECKING:
    from eggpool.db.connection import Database

logger = logging.getLogger(__name__)

_PRICE_SNAPSHOT_COLUMNS = """
    model_id, input_price_per_1k, output_price_per_1k, captured_at,
    input_per_million_microdollars, output_per_million_microdollars,
    cache_read_per_million_microdollars,
    cache_write_per_million_microdollars, source, provider_id
"""

_PRICE_NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[-+]?\d+)?")
_PER_TOKEN_UNITS = ("per token", "/token", "/tok")
_PER_1K_UNITS = ("per 1k", "/1k", "per k", "/k", "per thousand")
_PER_MILLION_UNITS = (
    "per 1m",
    "/1m",
    "per m",
    "/m",
    "per million",
    "per 1 million",
)


def _normalize_price_text(value: str) -> str:
    """Normalize human-entered price strings without making spacing significant."""
    return value.strip().lower().replace("$", "").replace(",", "").replace("_", "")


def _extract_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("price must be numeric, not boolean")
    if isinstance(value, int | float):
        number = Decimal(str(value))
    elif isinstance(value, str):
        normalized = _normalize_price_text(value)
        if not normalized:
            return None
        match = _PRICE_NUMBER_RE.search(normalized)
        if match is None:
            raise ValueError(f"price has no numeric value: {value!r}")
        try:
            number = Decimal(match.group(0))
        except InvalidOperation as exc:
            raise ValueError(f"price is not numeric: {value!r}") from exc
    else:
        raise ValueError(f"unsupported price type: {type(value).__name__}")

    if not number.is_finite():
        raise ValueError("price must be finite")
    if number < 0:
        raise ValueError("price must be non-negative")
    return number


def _price_unit(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(_normalize_price_text(value).split())
    compact = normalized.replace(" ", "")
    if any(unit in normalized or unit in compact for unit in _PER_TOKEN_UNITS):
        return "token"
    if any(unit in normalized or unit in compact for unit in _PER_1K_UNITS):
        return "1k"
    if any(unit in normalized or unit in compact for unit in _PER_MILLION_UNITS):
        return "million"
    return None


def parse_price_per_1k(value: object, *, default_unit: str = "1k") -> float | None:
    """Parse dollars-per-token/1K/million into legacy dollars-per-1K.

    Numeric inputs keep the caller's ``default_unit``. String inputs may include
    currency symbols, separators, and units such as ``$3 / 1M`` or
    ``0.000003 per token``; whitespace is ignored for unit detection.
    """
    number = _extract_decimal(value)
    if number is None:
        return None

    unit = _price_unit(value) or default_unit
    if unit == "token":
        number *= Decimal(1000)
    elif unit == "million":
        number /= Decimal(1000)
    elif unit != "1k":
        raise ValueError(f"unsupported price unit: {unit}")

    result = float(number)
    if not math.isfinite(result):
        raise ValueError("price must be finite")
    return result


def parse_microdollars_per_million(
    value: object, *, default_unit: str | None = None
) -> int | None:
    """Parse a cache rate into integer microdollars per million tokens.

    ``default_unit`` (``"token"``/``"1k"``/``"million"``/``None``) is
    applied only when the string carries no unit suffix. The default
    ``None`` matches the pre-existing contract: a bare numeric string is
    treated as already being in microdollars per million tokens. Use
    ``default_unit="token"`` for OpenRouter / Anthropic-style cache
    fields whose numeric form omits the unit (e.g.
    ``"0.000000021"`` = $0.000000021 per token).
    """
    number = _extract_decimal(value)
    if number is None:
        return None

    unit = _price_unit(value) or default_unit
    if unit == "token":
        # Dollars per token → microdollars per million tokens = × 10^12
        number *= Decimal(1_000_000) * Decimal(1_000_000)  # noqa: SIM114
    elif unit == "1k":
        # $X / 1K → $X*1000 / 1M → X*1000*1_000_000 microdollars/M
        number *= Decimal(1_000_000_000)
    elif unit == "million":
        number *= Decimal(1_000_000)
    elif unit is None:
        # No unit in the string and no default → assume already in
        # microdollars per million tokens. int() truncates fractional
        # sub-microdollar rates to zero, which matches the legacy
        # behaviour callers rely on.
        pass
    else:
        raise ValueError(f"unsupported price unit: {unit}")

    rounded = int(number.to_integral_value())
    if rounded < 0:
        raise ValueError("price must be non-negative")
    return rounded


def microdollars_per_million_from_price_per_1k(
    price_per_1k: float | None,
) -> int | None:
    """Convert legacy dollars/1K float pricing to integer microdollars/1M.

    Centralises the $0.003/1K → 3_000_000 conversion so callers do not
    embed the ``* 1_000_000_000`` magic number next to other arithmetic.
    Returns ``None`` when input is ``None`` (so callers can chain optional
    lookups without an extra ``is not None`` check).
    """
    if price_per_1k is None:
        return None
    return int(round(price_per_1k * 1_000_000_000))


# Cache category fallbacks used by CostCalculator when a per-token rate
# is missing but the model has nonzero tokens in that category. These
# are conservative local heuristics, intentionally higher than typical
# cache rates so that cost is over-reported rather than under-reported
# for partial snapshots.
_CACHE_READ_FALLBACK_PER_MILLION_MICRODOLLARS = 300_000  # $0.30 / 1M
_CACHE_WRITE_FALLBACK_PER_MILLION_MICRODOLLARS = 3_750_000  # $3.75 / 1M

_GENERIC_INPUT_FALLBACK_DOLLARS_PER_1K = 0.003  # $3 / 1M
_GENERIC_OUTPUT_FALLBACK_DOLLARS_PER_1K = 0.015  # $15 / 1M


def coerce_token_count(value: object) -> int:
    """Coerce a provider token count to a non-negative int.

    Accepts ints, numeric strings, and floats.  ``None``, empty
    strings, non-numeric strings, and negative values are silently
    treated as ``0`` so that malformed provider usage never raises
    during cost calculation.
    """
    if value is None:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return 0
        return max(0, int(value))
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return 0
        try:
            num = float(value)
        except (ValueError, OverflowError):
            return 0
        if not math.isfinite(num):
            return 0
        return max(0, int(num))
    return 0


def _normalize_token_count(value: int) -> int:
    """Normalize provider token counts before cost arithmetic."""
    return coerce_token_count(value)


@dataclass
class PriceSnapshot:
    """Price information for a model at a point in time."""

    model_id: str
    input_price_per_1k: float | None  # Legacy dollars/1K
    output_price_per_1k: float | None  # Legacy dollars/1K
    captured_at: str
    input_per_million_microdollars: int | None = None
    output_per_million_microdollars: int | None = None
    cache_read_per_million_microdollars: int | None = None
    cache_write_per_million_microdollars: int | None = None
    source: str = "upstream"
    provider_id: str = DEFAULT_PROVIDER_ID


class PriceRepository:
    """Repository for model price snapshots."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def record_snapshot(
        self,
        model_id: str,
        input_price_per_1k: float | None,
        output_price_per_1k: float | None,
        *,
        input_per_million_microdollars: int | None = None,
        output_per_million_microdollars: int | None = None,
        cache_read_per_million_microdollars: int | None = None,
        cache_write_per_million_microdollars: int | None = None,
        source: str = "config",
        provider_id: str = DEFAULT_PROVIDER_ID,
    ) -> None:
        """Record a price snapshot for a model.

        Must be called within a transaction context.
        """
        # Auto-convert legacy float to integer microdollars if not provided
        if input_per_million_microdollars is None and input_price_per_1k is not None:
            input_per_million_microdollars = int(
                round(input_price_per_1k * 1_000_000_000)
            )
        if output_per_million_microdollars is None and output_price_per_1k is not None:
            output_per_million_microdollars = int(
                round(output_price_per_1k * 1_000_000_000)
            )

        await self._db.execute_write(
            """
            INSERT INTO model_price_snapshots
                (model_id, input_price_per_1k, output_price_per_1k,
                 input_per_million_microdollars, output_per_million_microdollars,
                 cache_read_per_million_microdollars,
                 cache_write_per_million_microdollars, source, provider_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                model_id,
                input_price_per_1k,
                output_price_per_1k,
                input_per_million_microdollars,
                output_per_million_microdollars,
                cache_read_per_million_microdollars,
                cache_write_per_million_microdollars,
                source,
                provider_id,
            ),
        )

    async def get_latest_snapshot(
        self, model_id: str, provider_id: str | None = None
    ) -> PriceSnapshot | None:
        """Get the most recent price snapshot for a model.

        When ``provider_id`` is given, only that provider's pricing is
        considered. Falling back to another provider can silently charge a
        request using an unrelated upstream's rates.
        """
        if provider_id is not None:
            row = await self._db.fetch_one(
                f"SELECT {_PRICE_SNAPSHOT_COLUMNS} "
                "FROM model_price_snapshots "
                "WHERE model_id = ? AND provider_id = ? "
                "ORDER BY captured_at DESC, id DESC LIMIT 1",
                (model_id, provider_id),
            )
        else:
            row = await self._db.fetch_one(
                f"SELECT {_PRICE_SNAPSHOT_COLUMNS} "
                "FROM model_price_snapshots WHERE model_id = ? "
                "ORDER BY captured_at DESC, id DESC LIMIT 1",
                (model_id,),
            )
        if row is None:
            return None
        return self._row_to_snapshot(row)

    @staticmethod
    def _row_to_snapshot(row: Any) -> PriceSnapshot:
        """Convert a database row to a PriceSnapshot."""
        try:
            provider_id = row["provider_id"]
        except (IndexError, KeyError):
            provider_id = DEFAULT_PROVIDER_ID
        return PriceSnapshot(
            model_id=row["model_id"],
            input_price_per_1k=row["input_price_per_1k"],
            output_price_per_1k=row["output_price_per_1k"],
            captured_at=row["captured_at"],
            input_per_million_microdollars=row["input_per_million_microdollars"],
            output_per_million_microdollars=row["output_per_million_microdollars"],
            cache_read_per_million_microdollars=row[
                "cache_read_per_million_microdollars"
            ],
            cache_write_per_million_microdollars=row[
                "cache_write_per_million_microdollars"
            ],
            source=row["source"] if row["source"] is not None else "upstream",
            provider_id=provider_id,
        )

    async def get_snapshots_since(
        self,
        model_id: str,
        since_hours: int = 24,
        provider_id: str | None = None,
    ) -> list[PriceSnapshot]:
        """Get recent snapshots, optionally scoped to one provider."""
        if provider_id is None:
            rows = await self._db.fetch_all(
                f"SELECT {_PRICE_SNAPSHOT_COLUMNS} "
                "FROM model_price_snapshots "
                "WHERE model_id = ? "
                "AND captured_at > datetime('now', ? || ' hours') "
                "ORDER BY captured_at DESC, id DESC",
                (model_id, f"-{since_hours}"),
            )
        else:
            rows = await self._db.fetch_all(
                f"SELECT {_PRICE_SNAPSHOT_COLUMNS} "
                "FROM model_price_snapshots "
                "WHERE model_id = ? AND provider_id = ? "
                "AND captured_at > datetime('now', ? || ' hours') "
                "ORDER BY captured_at DESC, id DESC",
                (model_id, provider_id, f"-{since_hours}"),
            )
        return [self._row_to_snapshot(row) for row in rows]


class CostCalculator:
    """Calculates derived costs from token usage and price snapshots."""

    def __init__(self, price_repo: PriceRepository) -> None:
        self._price_repo = price_repo
        self._latest_cache: dict[tuple[str, str | None], PriceSnapshot | None] = {}

    def invalidate_price(self, model_id: str, provider_id: str | None = None) -> None:
        """Invalidate cached pricing after catalog persistence changes it."""
        self._latest_cache.pop((model_id, provider_id), None)
        # Provider-agnostic callers may observe any provider's newest row.
        self._latest_cache.pop((model_id, None), None)

    async def calculate_cost(
        self,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        provider_id: str | None = None,
    ) -> tuple[int, str]:
        """Calculate cost in microdollars from token usage.

        Returns:
            Tuple of (cost_microdollars, exactness_level). Exactness is
            one of ``"derived"`` (every nonzero billable category had a
            trusted rate), ``"partial"`` (at least one nonzero category
            had a trusted rate and at least one was filled by a category
            fallback), ``"estimated"`` (no trusted rates existed; the
            local heuristic priced the request), or ``"unknown"`` (no
            token usage at all).
        """
        input_tokens = _normalize_token_count(input_tokens)
        output_tokens = _normalize_token_count(output_tokens)
        cache_read_tokens = _normalize_token_count(cache_read_tokens)
        cache_write_tokens = _normalize_token_count(cache_write_tokens)

        total_tokens = (
            input_tokens + output_tokens + cache_read_tokens + cache_write_tokens
        )
        if total_tokens == 0:
            return 0, "unknown"

        cache_key = (model_id, provider_id)
        if cache_key not in self._latest_cache:
            self._latest_cache[cache_key] = await self._price_repo.get_latest_snapshot(
                model_id, provider_id=provider_id
            )
        snapshot = self._latest_cache[cache_key]

        if snapshot is None:
            return (
                self._estimate_cost(input_tokens, output_tokens),
                "estimated",
            )

        input_rate = snapshot.input_per_million_microdollars
        output_rate = snapshot.output_per_million_microdollars
        cache_read_rate = snapshot.cache_read_per_million_microdollars
        cache_write_rate = snapshot.cache_write_per_million_microdollars

        # Track which nonzero categories are missing a trusted rate so
        # we can fill them with a per-category fallback rather than
        # wholesale replacing the cost with a generic full-request
        # estimate. "Trusted" here means the snapshot has a non-None
        # microdollar rate for that category; legacy float rates are
        # converted on the fly via the snapshot's int fields.
        input_missing = input_tokens > 0 and input_rate is None
        output_missing = output_tokens > 0 and output_rate is None
        cache_read_missing = cache_read_tokens > 0 and cache_read_rate is None
        cache_write_missing = cache_write_tokens > 0 and cache_write_rate is None

        any_missing = (
            input_missing or output_missing or cache_read_missing or cache_write_missing
        )
        any_priced = (
            (input_tokens > 0 and not input_missing)
            or (output_tokens > 0 and not output_missing)
            or (cache_read_tokens > 0 and not cache_read_missing)
            or (cache_write_tokens > 0 and not cache_write_missing)
        )

        if any_missing and not any_priced:
            # Every nonzero category is missing a rate — fall back to
            # the generic full-request heuristic and label it estimated.
            return (
                self._estimate_cost(input_tokens, output_tokens),
                "estimated",
            )

        # Compute trusted-category cost using integer microdollar math.
        trusted_numerator = (
            (input_tokens * (input_rate or 0))
            + (output_tokens * (output_rate or 0))
            + (cache_read_tokens * (cache_read_rate or 0))
            + (cache_write_tokens * (cache_write_rate or 0))
        )
        trusted_cost = trusted_numerator // 1_000_000

        if any_missing:
            # Fill only the missing categories with a per-category
            # fallback. Mark exactness as "partial" so the dashboard
            # can distinguish this case from a fully-heuristic bill.
            fallback_cost = 0
            if input_missing:
                fallback_cost += int(
                    (input_tokens / 1000.0)
                    * _GENERIC_INPUT_FALLBACK_DOLLARS_PER_1K
                    * 1_000_000
                )
            if output_missing:
                fallback_cost += int(
                    (output_tokens / 1000.0)
                    * _GENERIC_OUTPUT_FALLBACK_DOLLARS_PER_1K
                    * 1_000_000
                )
            if cache_read_missing:
                fallback_cost += self._fallback_microdollars_for_category(
                    "cache_read", cache_read_tokens
                )
            if cache_write_missing:
                fallback_cost += self._fallback_microdollars_for_category(
                    "cache_write", cache_write_tokens
                )
            cost_microdollars = trusted_cost + fallback_cost
            return cost_microdollars, "partial"

        # All categories priced — pure derived cost. If the integer
        # microdollar arithmetic rounded a nonzero billable event down
        # to zero (e.g. an extremely cheap rate or tiny token count),
        # downgrade exactness so the request finalizer floors the cost
        # at the reservation estimate rather than recording zero.
        cost_microdollars = round(trusted_numerator / 1_000_000)
        exactness = "derived"
        if cost_microdollars == 0 and any(
            (
                input_tokens,
                output_tokens,
                cache_read_tokens,
                cache_write_tokens,
            )
        ):
            exactness = "estimated"
        return cost_microdollars, exactness

    def _estimate_cost(self, input_tokens: int, output_tokens: int) -> int:
        """Estimate cost when no price data is available.

        Uses rough estimates for common model tiers.
        """
        input_tokens = _normalize_token_count(input_tokens)
        output_tokens = _normalize_token_count(output_tokens)

        # Rough estimates in dollars per 1K tokens
        # These are fallback estimates - actual prices vary significantly
        estimated_input_price = _GENERIC_INPUT_FALLBACK_DOLLARS_PER_1K
        estimated_output_price = _GENERIC_OUTPUT_FALLBACK_DOLLARS_PER_1K

        input_cost = (input_tokens / 1000.0) * estimated_input_price
        output_cost = (output_tokens / 1000.0) * estimated_output_price
        total_cost = input_cost + output_cost

        return int(total_cost * 1_000_000)

    @staticmethod
    def _fallback_microdollars_for_category(
        category: str,
        tokens: int,
    ) -> int:
        """Category-specific fallback microdollars for partial snapshots.

        Used when ``calculate_cost`` has a snapshot but is missing the
        rate for a single nonzero token category. The fallback is
        intentionally conservative (over-reports) so partial snapshots
        cannot silently understate total cost.
        """
        if tokens <= 0:
            return 0
        if category == "cache_read":
            rate = _CACHE_READ_FALLBACK_PER_MILLION_MICRODOLLARS
        elif category == "cache_write":
            rate = _CACHE_WRITE_FALLBACK_PER_MILLION_MICRODOLLARS
        else:
            return 0
        return (tokens * rate) // 1_000_000
