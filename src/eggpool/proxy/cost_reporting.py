"""Provider-reported cost extraction from upstream response payloads.

This module parses authoritative cost values that some providers
embed in their usage payloads.  EggPool prefers these over locally
derived estimates whenever the upstream contract is unambiguous.

Only fields whose unit is explicit in the field name are accepted:

- ``*_usd`` / ``usd_*`` are treated as US dollars.
- ``*_microdollars`` / ``*_micros`` are treated as already-resolved
  microdollar integers.

Bare ``cost`` / ``total_cost`` fields are ambiguous (the unit is
unknown) and are rejected unless the provider's contract is on the
allowlist below.  When in doubt, the parser returns ``None`` rather
than guess — request finalization must not break because of a
malformed cost field.

Provider-specific aliases:

- ``opencode-go``: ``usage.cost`` and ``usage.total_cost`` are
  treated as US dollars because the OpenCode Go billing payload
  publishes billed request cost under those exact keys.  Only
  enable when the contract is confirmed for a live response.

The parser swallows every internal error (``TypeError``,
``ValueError``, ``AttributeError``, ``RecursionError``) and
returns ``None``.  Finalization paths can therefore call it
without exception handling.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from eggpool.constants import clamp_sqlite_integer

# Field paths inspected in priority order.  Microdollar fields
# resolve directly to an integer.  Dollar fields are multiplied
# by 1_000_000 and rounded.  Anything not listed here is ignored.
#
# Order matters: the first parseable field wins.  Microdollar
# paths precede dollar paths so callers do not pay for an
# unnecessary float roundtrip.
_DOLLAR_PATHS: tuple[tuple[str, ...], ...] = (
    ("usage", "cost_usd"),
    ("usage", "total_cost_usd"),
    ("usage", "billing", "cost_usd"),
    ("usage", "billing", "total_cost_usd"),
    ("billing", "cost_usd"),
    ("billing", "total_cost_usd"),
)

_MICRODOLLAR_PATHS: tuple[tuple[str, ...], ...] = (
    ("usage", "cost_microdollars"),
    ("usage", "total_cost_microdollars"),
)

# ``_micros`` is the conventional short form for already-resolved
# microdollar integers.  We still multiply by 1 here because the
# values are integer-valued by contract.
_MICROS_PATHS: tuple[tuple[str, ...], ...] = (
    ("usage", "cost_micros"),
    ("usage", "total_cost_micros"),
)


@dataclass(frozen=True, slots=True)
class ProviderReportedCost:
    """An authoritative cost reported by an upstream provider."""

    microdollars: int
    source: str


def _provider_dollar_alias_paths(
    provider_id: str | None,
) -> tuple[tuple[str, ...], ...]:
    """Return dollar field aliases permitted for ``provider_id``.

    Only providers with a confirmed billing contract that exposes
    cost under an unprefixed name are allowlisted here.  Adding a
    provider here is a deliberate contract assertion — verify
    against a live response before extending.
    """
    if provider_id == "opencode-go":
        return (("usage", "cost"), ("usage", "total_cost"))
    return ()


def _coerce_numeric(value: Any) -> Decimal | None:
    """Coerce ``value`` to a non-negative finite ``Decimal``.

    Accepts ints, floats, ``Decimal`` instances, and numeric strings.
    Rejects booleans, ``None``, negative numbers, NaN, infinities,
    and unparseable strings.  Returns ``None`` for any rejected
    value rather than raising.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, Decimal):
            number = value
        elif isinstance(value, int):
            number = Decimal(value)
        elif isinstance(value, float):
            if not math.isfinite(value):
                return None
            number = Decimal(str(value))
        elif isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                number = Decimal(text)
            except InvalidOperation:
                try:
                    number = Decimal(float(text))
                except (ValueError, OverflowError):
                    return None
                if not math.isfinite(float(number)):
                    return None
        else:
            return None
    except (ValueError, OverflowError, InvalidOperation):
        return None

    if not number.is_finite():
        return None
    if number < 0:
        return None
    return number


def _walk(data: Any, path: tuple[str, ...]) -> Any:
    """Walk ``path`` through nested dicts; return ``None`` on miss."""
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        mapping = cast("dict[str, Any]", current)
        current = mapping.get(key)
        if current is None:
            return None
    return current


def _format_source(path: tuple[str, ...]) -> str:
    return ".".join(path)


def _try_microdollar(
    data: Any, paths: tuple[tuple[str, ...], ...]
) -> ProviderReportedCost | None:
    for path in paths:
        raw = _walk(data, path)
        number = _coerce_numeric(raw)
        if number is None:
            continue
        return ProviderReportedCost(
            microdollars=clamp_sqlite_integer(int(number.to_integral_value())),
            source=_format_source(path),
        )
    return None


def _try_dollar(
    data: Any, paths: tuple[tuple[str, ...], ...]
) -> ProviderReportedCost | None:
    for path in paths:
        raw = _walk(data, path)
        number = _coerce_numeric(raw)
        if number is None:
            continue
        return ProviderReportedCost(
            microdollars=clamp_sqlite_integer(int(round(number * 1_000_000))),
            source=_format_source(path),
        )
    return None


def extract_provider_reported_cost(
    data: Any,
    *,
    provider_id: str | None,
    protocol: str,
) -> ProviderReportedCost | None:
    """Return a parsed provider-reported cost, or ``None`` when absent or unparseable.

    Inspects likely OpenAI-compatible usage locations:

    - ``usage.cost_microdollars`` / ``usage.cost_micros``
      / ``usage.total_cost_microdollars`` / ``usage.total_cost_micros``
    - ``usage.cost_usd`` / ``usage.total_cost_usd``
    - ``usage.billing.cost_usd`` / ``usage.billing.total_cost_usd``
    - ``billing.cost_usd`` / ``billing.total_cost_usd``

    Provider-specific aliases can be added when a contract is confirmed.
    Bare ``usage.cost`` is intentionally rejected unless ``provider_id``
    is on an explicit allowlist — ambiguity is too high.

    The ``protocol`` argument is accepted for future protocol-specific
    aliases; the generic field set is protocol-agnostic today.

    ``data`` does not need to be a dict; the parser is defensive and
    returns ``None`` for any unparseable structure.  All internal
    errors are swallowed so request finalization cannot break on a
    malformed cost field.
    """
    del protocol  # Reserved for future protocol-specific aliases.
    if not isinstance(data, dict):
        return None

    try:
        result = _try_microdollar(data, _MICRODOLLAR_PATHS)
        if result is not None:
            return result

        result = _try_microdollar(data, _MICROS_PATHS)
        if result is not None:
            return result

        result = _try_dollar(data, _DOLLAR_PATHS)
        if result is not None:
            return result

        alias_paths = _provider_dollar_alias_paths(provider_id)
        if alias_paths:
            result = _try_dollar(data, alias_paths)
            if result is not None:
                return result

    except (TypeError, ValueError, AttributeError, RecursionError):
        return None

    return None


__all__ = [
    "ProviderReportedCost",
    "extract_provider_reported_cost",
]
