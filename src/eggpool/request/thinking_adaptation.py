"""Post-selection thinking control adaptation.

Extracted from ``RequestCoordinator`` in Plan 136 Phase 5.  This module
owns provider-specific thinking control resolution, budget recompute,
and control normalization — the post-selection preparation stage that
runs after account selection but before upstream dispatch.

Design rules
~~~~~~~~~~~~
- Functions receive their dependencies explicitly; no coordinator
  self-references.
- ``ProviderBoundRequest`` is passed by reference and mutated in place
  through its narrow ownership API (``adopt_provider_payload``,
  ``mutate_top_level_mapping``).
- Strict-policy rejections propagate as ``CapabilityError``; callers
  must finalize the attempt before re-raising.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from eggpool.catalog.capabilities import (
    ThinkingCapability,
    dict_to_model_capabilities,
)

if TYPE_CHECKING:
    from eggpool.request.provider_bound_request import ProviderBoundRequest

logger = logging.getLogger(__name__)


def resolve_selected_thinking_capability(
    catalog: Any,  # noqa: ANN401
    model_id: str,
    provider_id: str,
) -> ThinkingCapability:
    """Best-effort lookup of the selected provider's thinking capability.

    Returns :class:`ThinkingCapability` with status ``"unknown"`` when
    the provider entry is missing or carries no capability metadata.
    Used by post-selection helpers to apply provider-specific
    ``effort_to_budget_tokens`` overrides and min/max clamps for
    collapsed model ids.
    """
    entry = catalog.cache.get_provider_model_entry(model_id, provider_id)
    if entry is None:
        return ThinkingCapability()
    caps_raw: object = entry.get("capabilities")  # pyright: ignore[reportUnknownMemberType]
    if not isinstance(caps_raw, dict):
        return ThinkingCapability()
    caps_dict: dict[str, object] = caps_raw  # pyright: ignore[reportUnknownVariableType]
    return dict_to_model_capabilities(caps_dict).thinking


def client_has_thinking_controls(
    original_body: bytes,
    protocol: str,
    *,
    parsed_payload: Any | None = None,  # noqa: ANN401
) -> bool:
    """Return True when the client request contains thinking/reasoning controls.

    Checks for OpenAI-style ``reasoning_effort`` or Anthropic-style
    ``thinking`` / ``thinking_budget`` fields.  Used by the prepared
    transcode reuse logic to decide whether the cached preflight
    translation is safe to skip — thinking budget resolution depends
    on provider-specific capability lookup, which is not available
    during preflight.
    """
    from eggpool.jsonx import loads as jsonx_loads

    if parsed_payload is not None:
        body_obj: object | None = parsed_payload.parsed_dict
    else:
        try:
            body_obj = jsonx_loads(original_body)
        except ValueError:
            return False
    if not isinstance(body_obj, dict):
        return False
    body: dict[str, object] = body_obj  # pyright: ignore[reportUnknownVariableType]
    if isinstance(body.get("reasoning_effort"), str):
        return True
    thinking_obj = body.get("thinking")
    if isinstance(thinking_obj, dict) and "budget_tokens" in thinking_obj:
        return True
    return body.get("thinking_budget") is not None


def recompute_thinking_budget_for_provider(
    *,
    context: Any,  # ProxyRequestContext  # noqa: ANN401
    selected: Any,  # SelectedAttempt  # noqa: ANN401
    thinking_capability: ThinkingCapability,
    request: ProviderBoundRequest,
    transcoder_policy: Any | None = None,  # noqa: ANN401
) -> None:
    """Re-resolve ``thinking.budget_tokens`` for the selected provider.

    The preflight translation in ``execute()`` uses the collapsed
    (best-effort) capability, which may under- or over-restrict
    thinking budgets for provider-specific overrides.  This helper
    runs :func:`resolve_thinking_budget` against the selected
    provider's capability and overwrites the ``thinking`` block in
    the provider-bound payload with the resolved budget.

    Resolution uses the **original** client thinking controls
    (``reasoning_effort`` for OpenAI, explicit ``thinking.budget_tokens``
    for Anthropic), not the already-translated provider payload
    budget.  Forwarding the translated value would short-circuit the
    resolver's effort mapping because ``requested_budget_tokens``
    is consulted before ``requested_effort``; for OpenAI clients
    that would prevent the selected provider's
    ``effort_to_budget_tokens`` override from taking effect.

    Strict-policy rejections propagate as :class:`CapabilityError`
    so the client receives an HTTP 400 before any upstream dispatch.
    Callers must wrap invocations with the coordinator's
    ``_finalize_selected_capability_rejection`` so durable
    attempt state and runtime counters are cleaned up before the
    error is re-raised.
    """
    from eggpool.transcoder.budget_resolver import resolve_thinking_budget

    if not context.transcode_required:
        return
    if transcoder_policy is not None and not getattr(
        transcoder_policy.features, "thinking", False
    ):
        return
    thinking_block_obj: object = request.provider_payload.get("thinking")
    budget_defaults: dict[str, int] | None = None
    policy = "lenient"
    if transcoder_policy is not None:
        budget_defaults = transcoder_policy.thinking_budget_defaults.as_dict()
        policy = transcoder_policy.budget_resolution_policy
    original_effort, original_budget = _extract_original_thinking_budget_inputs(
        context,
    )
    if (
        original_effort is None
        and original_budget is None
        and not isinstance(thinking_block_obj, dict)
    ):
        return
    resolution = resolve_thinking_budget(
        model_id=context.model_id,
        provider_id=selected.provider_id,
        requested_effort=original_effort,
        requested_budget_tokens=original_budget,
        capability=thinking_capability,
        budget_defaults=budget_defaults,
        budget_resolution_policy=policy,
    )
    if context.thinking_trace is not None:
        context.thinking_trace["resolved_budget_tokens"] = resolution.budget_tokens
        context.thinking_trace["budget_resolution_source"] = resolution.source
        context.thinking_trace["capability_status"] = thinking_capability.status
        context.thinking_trace["capability_source"] = thinking_capability.source
    if resolution.thinking_enabled and resolution.budget_tokens is not None:
        if context.thinking_trace is not None and not context.thinking_trace.get(
            "upstream_fields"
        ):
            context.thinking_trace["upstream_fields"] = ["thinking"]
        if isinstance(thinking_block_obj, dict):
            request.mutate_top_level_mapping(
                "thinking",
                "budget_tokens",
                resolution.budget_tokens,
                reason="thinking_budget",
            )
        else:
            provider_payload = dict(request.provider_payload)
            provider_payload["thinking"] = {
                "type": "enabled",
                "budget_tokens": resolution.budget_tokens,
            }
            request.adopt_provider_payload(
                provider_payload,
                reason="thinking_budget",
            )
    elif "thinking" in request.provider_payload:
        if context.thinking_trace is not None and resolution.source in {
            "reasoning_disabled",
            "unmapped_effort_dropped",
        }:
            context.thinking_trace["upstream_fields"] = []
        provider_payload = dict(request.provider_payload)
        del provider_payload["thinking"]
        request.adopt_provider_payload(
            provider_payload,
            reason="thinking_budget_disabled",
        )
    if context.transcode_context is not None:
        context.transcode_context.loss_warnings.extend(resolution.warnings)
        if resolution.clamped:
            context.transcode_context.loss_warnings.append(
                {
                    "kind": "budget_clamped",
                    "reason": "provider_specific_override",
                    "resolved": resolution.budget_tokens,
                    "provider_id": selected.provider_id,
                }
            )


def adapt_provider_thinking_controls(
    *,
    context: Any,  # ProxyRequestContext  # noqa: ANN401
    selected: Any,  # SelectedAttempt  # noqa: ANN401
    thinking_capability: ThinkingCapability,
    request: ProviderBoundRequest,
    catalog: Any,  # CatalogService  # noqa: ANN401
    config: Any | None = None,  # AppConfig  # noqa: ANN401
    transcoder_policy: Any | None = None,  # noqa: ANN401
    resolve_provider_kind_fn: Any | None = None,  # noqa: ANN401
) -> None:
    """Validate and adapt thinking controls against the provider contract.

    Runs after budget recompute and before upstream dispatch.  Uses the
    original client intent (Workstream D) rather than re-reading
    already-translated fields.  On rejection, raises
    :class:`CapabilityError` so callers can finalize the attempt.

    This stage runs for both native and transcoded paths, and for
    both streaming and non-streaming requests.  When the client
    protocol matches the upstream protocol (native path), unknown
    contracts pass through — the upstream will reject if needed.
    """
    from eggpool.catalog.capabilities import ThinkingRequestIntent
    from eggpool.transcoder.builtin_contracts import resolve_control_contract
    from eggpool.transcoder.provider_adaptation import (
        ProviderControlPolicy,
        adapt_thinking_controls,
    )

    intent = context.thinking_intent
    if not isinstance(intent, ThinkingRequestIntent):
        return
    if not intent.client_requests_new_reasoning:
        return

    # Resolve the effective control contract.
    provider_url = ""
    if selected.provider_id:
        entry = catalog.cache.get_provider_model_entry(
            context.model_id,
            selected.provider_id,
        )
        if entry is not None:
            provider_url = str(entry.get("base_url", ""))

    # Resolve provider kind for contract matching.
    provider_kind = None
    if resolve_provider_kind_fn is not None:
        provider_kind = resolve_provider_kind_fn(catalog, selected, config)

    contract = resolve_control_contract(
        capability=thinking_capability,
        provider_id=selected.provider_id or "",
        provider_kind=provider_kind,
        provider_base_url=provider_url,
        model_id=context.model_id,
        protocol=context.upstream_protocol or context.protocol,
    )

    # For native paths (client protocol == upstream protocol), only
    # apply normalization when we have a definitive contract.  Unknown
    # contracts pass through — the upstream will reject if needed.
    is_native = context.protocol == (context.upstream_protocol or context.protocol)
    if is_native and contract.mode == "unknown":
        return

    # Build the adaptation policy from config.
    policy = ProviderControlPolicy()
    if transcoder_policy is not None and hasattr(
        transcoder_policy,
        "provider_control_policy",
    ):
        pcp = transcoder_policy.provider_control_policy
        policy = ProviderControlPolicy(
            unsupported_control=pcp.unsupported_control,
            unknown_contract=pcp.unknown_contract,
            allow_compatibility_retry=pcp.allow_compatibility_retry,
        )

    # Pass the current provider-bound payload read-only.  The adapter
    # builds its own shallow-copied working root and returns a fresh
    # dict that shares unaffected descendants (messages/tools/etc.)
    # with the source.
    payload_obj = request.provider_payload

    # Override the capability's control_contract with the resolved one.
    adapted_capability = thinking_capability.model_copy(deep=True)
    adapted_capability.control_contract = contract

    result = adapt_thinking_controls(
        payload=payload_obj,
        client_protocol=context.protocol,
        model_id=context.model_id,
        provider_id=selected.provider_id or "",
        capability=adapted_capability,
        intent=intent,
        policy=policy,
    )

    # Update the thinking trace with adaptation results.
    if context.thinking_trace is not None:
        context.thinking_trace["provider_control_decision"] = result.decision
        context.thinking_trace["provider_control_warnings"] = [
            {"kind": w.kind, "detail": w.detail, "field": w.field_name}
            for w in result.warnings
        ]
        if result.changed:
            context.thinking_trace["upstream_fields"] = list(
                result.emitted_controls,
            )

    # If the adaptation changed the payload, adopt the result
    # through the trusted narrow boundary.
    if result.changed:
        request.adopt_provider_payload(
            result.payload,
            reason="thinking_control",
        )


def _extract_original_thinking_budget_inputs(
    context: Any,  # ProxyRequestContext  # noqa: ANN401
) -> tuple[str | None, int | None]:
    """Extract the original client thinking controls from ``context.original_body``.

    Returns ``(requested_effort, requested_budget_tokens)`` so callers can
    distinguish an OpenAI-style ``reasoning_effort`` request from an
    Anthropic-style explicit ``thinking.budget_tokens`` request.

    The post-selection budget recompute must resolve against the original
    client intent rather than the already-translated provider payload,
    because the resolver prioritises an explicit ``requested_budget_tokens``
    value over the capability's ``effort_to_budget_tokens`` mapping.
    """
    from eggpool.jsonx import loads as jsonx_loads

    payload_cache = context.parsed_payload
    if payload_cache is not None:
        original_body_obj: object | None = payload_cache.parsed_dict
    else:
        try:
            original_body_obj = jsonx_loads(context.original_body)
        except ValueError:
            return (None, None)
    if not isinstance(original_body_obj, dict):
        return (None, None)
    original_body: dict[str, object] = original_body_obj  # pyright: ignore[reportUnknownVariableType]
    effort_obj: object = original_body.get("reasoning_effort")  # pyright: ignore[reportUnknownMemberType]
    if isinstance(effort_obj, str) and effort_obj:
        return (effort_obj, None)
    thinking_obj: object = original_body.get("thinking")  # pyright: ignore[reportUnknownMemberType]
    if isinstance(thinking_obj, dict):
        thinking_dict: dict[str, object] = thinking_obj  # pyright: ignore[reportUnknownVariableType]
        budget_obj: object = thinking_dict.get("budget_tokens")  # pyright: ignore[reportUnknownMemberType]
        if isinstance(budget_obj, int) and not isinstance(budget_obj, bool):
            return (None, int(budget_obj))
    budget_obj = original_body.get("thinking_budget")  # pyright: ignore[reportUnknownMemberType]
    if isinstance(budget_obj, int) and not isinstance(budget_obj, bool):
        return (None, int(budget_obj))
    return (None, None)
