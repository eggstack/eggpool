"""Usage extraction helpers extracted from RequestCoordinator."""

from __future__ import annotations

import logging
from typing import Any, cast

from eggpool.jsonx import loads as jsonx_loads
from eggpool.proxy.usage import (
    StreamUsageResult,
    extract_anthropic_response_usage,
    extract_openai_response_usage,
)

logger = logging.getLogger(__name__)


def extract_non_stream_usage(
    protocol: str,
    body: bytes,
    *,
    provider_id: str | None = None,
) -> StreamUsageResult | None:
    """Extract usage from a non-streaming response body.

    ``provider_id`` enables provider-specific aliases when parsing
    an authoritative cost field (e.g. OpenCode Go's bare
    ``usage.cost`` field). The parser is defensive and returns
    ``None`` for absent or unparseable cost values; the finalizer
    will fall back to locally derived cost in that case.
    """
    try:
        data = jsonx_loads(body)
    except ValueError:
        logger.warning(
            "Non-streaming upstream response body is not valid JSON; "
            "usage will not be extracted (body_len=%d)",
            len(body),
        )
        return None

    if not isinstance(data, dict):
        logger.debug(
            "Non-streaming upstream response is not a JSON object "
            "(type=%s); usage will not be extracted",
            type(data).__name__,
        )
        return None

    data_dict = cast("dict[str, Any]", data)

    if protocol == "anthropic":
        return extract_anthropic_response_usage(
            data_dict,
            provider_id=provider_id,
        )

    return extract_openai_response_usage(
        data_dict,
        provider_id=provider_id,
    )


def extract_non_stream_usage_from_parsed(
    protocol: str,
    parsed: Any,  # ParsedUpstreamResponse
    *,
    provider_id: str | None = None,
) -> StreamUsageResult | None:
    """Extract usage from an already-parsed upstream response.

    Plan 028: reads from ``parsed.parsed_dict`` instead of
    re-parsing raw bytes, eliminating the duplicate decode in the
    non-streaming success path.  Falls back to the byte-accepting
    wrapper when parsing has not yet been attempted or failed.
    """
    data_dict = parsed.parsed_dict
    if data_dict is None:
        # ``parsed_dict`` has already attempted the one permitted JSON
        # decode.  Invalid/non-object bodies are valid native pass-through
        # responses, but they do not contain extractable usage.
        return None

    if protocol == "anthropic":
        return extract_anthropic_response_usage(
            data_dict,
            provider_id=provider_id,
        )

    return extract_openai_response_usage(
        data_dict,
        provider_id=provider_id,
    )
