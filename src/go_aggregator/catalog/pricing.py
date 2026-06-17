"""Price snapshot storage and derived cost calculation."""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from go_aggregator.db.connection import Database

logger = logging.getLogger(__name__)

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


def parse_microdollars_per_million(value: object) -> int | None:
    """Parse a cache rate into integer microdollars per million tokens."""
    number = _extract_decimal(value)
    if number is None:
        return None

    unit = _price_unit(value)
    if unit == "token":
        # Dollars per token → microdollars per million tokens = × 10^12
        number *= Decimal(1_000_000) * Decimal(1_000_000)  # noqa: SIM114
    elif unit in ("1k", "million"):
        number *= Decimal(1_000_000)

    rounded = int(number.to_integral_value())
    if rounded < 0:
        raise ValueError("price must be non-negative")
    return rounded


def _normalize_token_count(value: int) -> int:
    """Normalize provider token counts before cost arithmetic."""
    return max(0, int(value))


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
                 cache_write_per_million_microdollars, source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )

    async def get_latest_snapshot(self, model_id: str) -> PriceSnapshot | None:
        """Get the most recent price snapshot for a model."""
        row = await self._db.fetch_one(
            """
            SELECT model_id, input_price_per_1k, output_price_per_1k,
                   captured_at, input_per_million_microdollars,
                   output_per_million_microdollars,
                   cache_read_per_million_microdollars,
                   cache_write_per_million_microdollars, source
            FROM model_price_snapshots
            WHERE model_id = ?
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            (model_id,),
        )
        if row is None:
            return None
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
        )

    async def get_snapshots_since(
        self, model_id: str, since_hours: int = 24
    ) -> list[PriceSnapshot]:
        """Get price snapshots for a model since the given time."""
        rows = await self._db.fetch_all(
            """
            SELECT model_id, input_price_per_1k, output_price_per_1k,
                   captured_at, input_per_million_microdollars,
                   output_per_million_microdollars,
                   cache_read_per_million_microdollars,
                   cache_write_per_million_microdollars, source
            FROM model_price_snapshots
            WHERE model_id = ? AND captured_at > datetime('now', ? || ' hours')
            ORDER BY captured_at DESC
            """,
            (model_id, f"-{since_hours}"),
        )
        return [
            PriceSnapshot(
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
            )
            for row in rows
        ]


class CostCalculator:
    """Calculates derived costs from token usage and price snapshots."""

    def __init__(self, price_repo: PriceRepository) -> None:
        self._price_repo = price_repo

    async def calculate_cost(
        self,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> tuple[int, str]:
        """Calculate cost in microdollars from token usage.

        Returns:
            Tuple of (cost_microdollars, exactness_level)
        """
        input_tokens = _normalize_token_count(input_tokens)
        output_tokens = _normalize_token_count(output_tokens)
        cache_read_tokens = _normalize_token_count(cache_read_tokens)
        cache_write_tokens = _normalize_token_count(cache_write_tokens)

        snapshot = await self._price_repo.get_latest_snapshot(model_id)

        if snapshot is None:
            return (
                self._estimate_cost(input_tokens, output_tokens),
                "estimated",
            )

        input_rate = snapshot.input_per_million_microdollars
        output_rate = snapshot.output_per_million_microdollars
        cache_read_rate = snapshot.cache_read_per_million_microdollars
        cache_write_rate = snapshot.cache_write_per_million_microdollars

        # Determine if required rates are missing for nonzero token categories
        missing_required_rate = (
            (input_tokens > 0 and input_rate is None)
            or (output_tokens > 0 and output_rate is None)
            or (cache_read_tokens > 0 and cache_read_rate is None)
            or (cache_write_tokens > 0 and cache_write_rate is None)
        )

        if missing_required_rate:
            # Use fallback estimation for missing categories
            exactness = "estimated"
            # Use available rates where possible, fallback to estimate for missing
            input_cost = (input_tokens * (input_rate or 0)) // 1_000_000
            output_cost = (output_tokens * (output_rate or 0)) // 1_000_000
            cache_read_cost = (cache_read_tokens * (cache_read_rate or 0)) // 1_000_000
            cache_write_cost = (
                cache_write_tokens * (cache_write_rate or 0)
            ) // 1_000_000
            calculated_partial = (
                input_cost + output_cost + cache_read_cost + cache_write_cost
            )
            # Fall back to at least the estimated cost
            fallback = self._estimate_cost(input_tokens, output_tokens)
            cost_microdollars = max(calculated_partial, fallback)
        else:
            exactness = "derived"
            total_numerator = (
                (input_tokens * (input_rate or 0))
                + (output_tokens * (output_rate or 0))
                + (cache_read_tokens * (cache_read_rate or 0))
                + (cache_write_tokens * (cache_write_rate or 0))
            )
            cost_microdollars = round(total_numerator / 1_000_000)
            # If the integer microdollar arithmetic rounded a nonzero
            # billable event down to zero, the result is not actually
            # "derived" (i.e., exact) — it is a lower bound on the
            # true cost. Downgrade exactness so the request finalizer
            # floors the cost at the reservation estimate.
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
        estimated_input_price = 0.003  # $3 per 1M input tokens
        estimated_output_price = 0.015  # $15 per 1M output tokens

        input_cost = (input_tokens / 1000.0) * estimated_input_price
        output_cost = (output_tokens / 1000.0) * estimated_output_price
        total_cost = input_cost + output_cost

        return int(total_cost * 1_000_000)
