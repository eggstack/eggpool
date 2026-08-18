"""Upstream URL resolution and endpoint validation.

Extracted from ``RequestCoordinator`` in Plan 136 Phase 5.  These
functions resolve the absolute upstream URL from provider configuration
and validate that the client endpoint matches the model's supported
protocols.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def get_upstream_url(
    protocol: str,
    provider_id: str | None = None,
    *,
    config: Any | None = None,  # noqa: ANN401
) -> str:
    """Get the absolute upstream URL for a protocol and provider.

    When a provider configuration is available, uses
    ``compose_provider_url()`` to combine ``base_url`` with the
    configured protocol-specific path so all outbound dispatch
    paths share the same URL composition rules as catalog fetch.
    Falls back to bare paths when no provider config is loaded.
    """
    from eggpool.providers.contract import compose_provider_url

    if provider_id and config is not None:
        provider_cfg = config.providers.get(provider_id)
        if provider_cfg is not None:
            path = (
                provider_cfg.anthropic_path
                if protocol == "anthropic"
                else provider_cfg.openai_path
            )
            return compose_provider_url(provider_cfg, path)
    if protocol == "anthropic":
        return "/messages"
    return "/chat/completions"


def resolve_upstream_protocol(
    context: Any,  # ProxyRequestContext  # noqa: ANN401
    *,
    catalog: Any,  # CatalogService  # noqa: ANN401
    transcoder_policy: Any | None = None,  # noqa: ANN401
) -> str | None:
    """Determine the upstream protocol for transcoding.

    Returns the protocol to use upstream, or None when no
    transcodable route exists.  When the client protocol matches
    a resolved model protocol, returns that protocol directly
    (native match, no transcoding needed).

    Translation is on by default. ``transcoder_policy.enabled`` is
    a deprecated escape hatch — only an explicit ``False`` disables
    translation (restoring the legacy protocol-exact routing). ``None``
    and ``True`` both allow transcoding, so a missing policy object
    never silently disables it.
    """
    model_protocols = catalog.cache.get_model_protocols(
        context.model_id,
        provider_id=context.provider_id,
    )
    if context.protocol in model_protocols:
        return context.protocol  # native match

    if transcoder_policy is not None and transcoder_policy.enabled is False:
        return None  # legacy protocol-exact behaviour (escape hatch)

    # Find transcodable protocols among all eligible accounts.
    candidates = catalog.cache.get_transcodable_protocols(
        context.model_id,
        client_protocol=context.protocol,
        provider_id=context.provider_id,
    )
    if not candidates:
        return None

    # Choose the protocol with the largest eligible-account set.
    counts = {
        p: catalog.cache.count_eligible_accounts_for_protocol(
            context.model_id,
            p,
            provider_id=context.provider_id,
        )
        for p in candidates
    }
    # Prefer the protocol with the most eligible accounts;
    # ties broken by alphabetical order.
    return max(sorted(counts), key=lambda p: counts[p])


def validate_endpoint_or_transcode(
    context: Any,  # ProxyRequestContext  # noqa: ANN401
    *,
    catalog: Any,  # CatalogService  # noqa: ANN401
    transcoder_policy: Any | None = None,  # noqa: ANN401
) -> None:
    """Validate that the endpoint matches the model's protocol.

    When the client protocol does not match the model's native
    protocol but a transcodable route exists (transcoder enabled and
    an account supports the native protocol), the mismatch is
    accepted and ``upstream_protocol`` / ``transcode_required`` are
    set on the context.

    Raises ProtocolMismatchError (which callers render as 400) when
    the wrong endpoint is used for a known model and no transcodable
    route exists.
    """
    from eggpool.catalog.protocols import ModelProtocolResolver
    from eggpool.errors import ModelNotFoundError, ModelUnavailableError

    if not catalog.cache.has_model(context.model_id):
        raise ModelNotFoundError(context.model_id)

    model_protocols = catalog.cache.get_model_protocols(
        context.model_id,
        provider_id=context.provider_id,
    )
    if not model_protocols:
        # Unresolved protocol - fail closed
        raise ModelUnavailableError(
            f"Model {context.model_id!r} has unresolved protocol"
        )

    if context.protocol in model_protocols:
        return

    # Check if transcoding can bridge the protocol gap.
    upstream_protocol = resolve_upstream_protocol(
        context, catalog=catalog, transcoder_policy=transcoder_policy
    )
    if upstream_protocol is not None:
        context.upstream_protocol = upstream_protocol
        context.transcode_required = True
        # Sync the transcode_context so execute() can detect the
        # protocol mismatch and select the correct transcoder.
        if context.transcode_context is not None:
            context.transcode_context.upstream_protocol = upstream_protocol
        return

    resolver = ModelProtocolResolver()
    model_protocol = sorted(model_protocols)[0]
    resolver.validate_endpoint(model_protocol, context.protocol, context.model_id)
