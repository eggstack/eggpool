"""Preparation for internal concrete requests that reuse the coordinator."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from eggpool.catalog.capabilities import classify_thinking_request
from eggpool.jsonx import dumps_bytes
from eggpool.request.coordinator import ProxyRequestContext
from eggpool.request.limits import estimate_reservation_tokens
from eggpool.request.parsed_payload import ParsedRequestPayload
from eggpool.request.provider_bound_request import ProviderBoundRequest
from eggpool.routing.provider import parse_model_provider
from eggpool.transcoder.context import TranscodeContext
from eggpool.wire.ir import canonical_request_from_mapping

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping


def prepare_internal_concrete_request(
    payload: Mapping[str, Any],
    *,
    model_id: str,
    known_provider_ids: Collection[str] = (),
    request_id: str | None = None,
    incoming_headers: Mapping[str, str] | None = None,
    client_ip: str = "",
) -> ProxyRequestContext:
    """Build a non-streaming OpenAI Chat context for a known concrete model.

    This is intentionally below the public FastAPI handler.  It performs the
    same canonical/provider-bound construction needed by the coordinator but
    does not perform catalog access, account routing, or network I/O.
    """
    supplied_model = payload.get("model")
    if not isinstance(supplied_model, str) or not supplied_model.strip():
        raise ValueError("internal request payload requires a model")
    normalized_model, provider_id = parse_model_provider(
        supplied_model,
        known_provider_ids,
    )
    if normalized_model != model_id:
        raise ValueError(
            f"internal request model {normalized_model!r} does not match {model_id!r}"
        )
    body_payload = dict(payload)
    body_payload["model"] = model_id
    body_payload["stream"] = False
    body = dumps_bytes(body_payload)
    parsed_payload = ParsedRequestPayload(original_bytes=body)
    parsed_payload.set_parsed_dict(body_payload)
    canonical = canonical_request_from_mapping(
        body_payload,
        client_surface="chat_completions",
        protocol="openai",
    )
    request_uuid = request_id or str(uuid.uuid4())
    provider_bound = ProviderBoundRequest(
        client_bytes=body,
        client_payload=body_payload,
        client_protocol="openai",
        model_id=model_id,
        parsed_payload=parsed_payload,
    )
    transcode_context = TranscodeContext(
        request_id=request_uuid,
        client_protocol="openai",
        upstream_protocol="openai",
        client_surface="chat_completions",
        selected_wire_surface="openai_chat_completions",
        canonical_request=canonical,
        reasoning_intent=canonical.reasoning,
    )
    return ProxyRequestContext(
        request_id=request_uuid,
        protocol="openai",
        model_id=model_id,
        streaming=False,
        original_body=body,
        incoming_headers=dict(incoming_headers or {}),
        client_ip=client_ip,
        provider_id=provider_id,
        upstream_protocol="openai",
        client_surface="chat_completions",
        selected_wire_surface="openai_chat_completions",
        canonical_request=canonical,
        reasoning_intent=canonical.reasoning,
        transcode_context=transcode_context,
        estimated_reservation_tokens=estimate_reservation_tokens(body),
        thinking_requirement=classify_thinking_request(body_payload, "openai"),
        parsed_payload=parsed_payload,
        provider_bound=provider_bound,
    )


__all__ = ["prepare_internal_concrete_request"]
