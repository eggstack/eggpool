"""Per-model protocol resolution.

Resolution order (highest priority first):
1. Explicit TOML override
2. Explicit per-model metadata from upstream
3. Exact known-model mapping
4. Known family mapping
5. Previously persisted protocol
6. Unresolved error
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from eggpool.models.config import AppConfig

logger = logging.getLogger(__name__)

ProtocolName = Literal["openai", "anthropic"]
SUPPORTED_PROTOCOLS: frozenset[ProtocolName] = frozenset({"openai", "anthropic"})

# Exact known-model -> protocol mappings
EXACT_MODEL_PROTOCOLS: dict[str, str] = {
    "gpt-4": "openai",
    "gpt-4-turbo": "openai",
    "gpt-4o": "openai",
    "gpt-4o-mini": "openai",
    "gpt-3.5-turbo": "openai",
    "claude-3-opus-20240229": "anthropic",
    "claude-3-sonnet-20240229": "anthropic",
    "claude-3-haiku-20240307": "anthropic",
    "claude-3-5-sonnet-20241022": "anthropic",
    "claude-3-5-haiku-20241022": "anthropic",
}

# Known family prefix -> protocol mappings
FAMILY_PROTOCOLS: dict[str, str] = {
    "gpt-": "openai",
    "o1-": "openai",
    "o3-": "openai",
    "claude-": "anthropic",
    "glm-": "openai",
    "kimi-": "openai",
    "mimo-": "openai",
    "deepseek-": "openai",
    "minimax-": "anthropic",
    "qwen3": "anthropic",
}


@dataclass
class ProtocolResolution:
    """Result of resolving a model's protocol."""

    protocol: str
    source: str  # "config_override", "upstream_metadata", "exact_mapping",
    # "family_mapping", "persisted", "unresolved"
    endpoint_path: str | None = None


class ModelProtocolResolver:
    """Resolves the protocol family for individual models.

    Uses a priority-ordered resolution chain to determine whether a model
    uses the OpenAI or Anthropic protocol.
    """

    def __init__(self, config: AppConfig | None = None) -> None:
        self._config = config
        self._config_overrides: dict[str, str] = {}
        if config is not None:
            for model_id, override in config.model_overrides.items():
                if override.protocol is not None:
                    self._config_overrides[model_id] = override.protocol

    def resolve_from_metadata(
        self,
        model_id: str,
        source_metadata: dict[str, object],
    ) -> ProtocolResolution:
        """Resolve protocol from upstream metadata.

        Checks fields like 'api_type', 'protocol', 'family' in source_metadata.
        """
        # Step 1: Explicit TOML override
        if model_id in self._config_overrides:
            return ProtocolResolution(
                protocol=self._config_overrides[model_id],
                source="config_override",
            )

        # Step 2: Explicit per-model metadata from upstream
        api_type = str(source_metadata.get("api_type", ""))
        if api_type in SUPPORTED_PROTOCOLS:
            return ProtocolResolution(
                protocol=api_type,
                source="upstream_metadata",
            )
        protocol_field = str(source_metadata.get("protocol", ""))
        if protocol_field in SUPPORTED_PROTOCOLS:
            return ProtocolResolution(
                protocol=protocol_field,
                source="upstream_metadata",
            )

        return ProtocolResolution(protocol="", source="unresolved")

    def resolve_from_catalog(
        self,
        model_id: str,
        persisted_protocol: str | None = None,
    ) -> ProtocolResolution:
        """Resolve protocol from catalog knowledge.

        Checks exact mapping, family mapping, then persisted value.
        """
        # Step 1: Explicit TOML override
        if model_id in self._config_overrides:
            return ProtocolResolution(
                protocol=self._config_overrides[model_id],
                source="config_override",
            )

        # Step 3: Exact known-model mapping
        if model_id in EXACT_MODEL_PROTOCOLS:
            return ProtocolResolution(
                protocol=EXACT_MODEL_PROTOCOLS[model_id],
                source="exact_mapping",
            )

        # Step 4: Known family mapping
        model_lower = model_id.lower()
        for prefix, proto in FAMILY_PROTOCOLS.items():
            if model_lower.startswith(prefix.lower()):
                return ProtocolResolution(
                    protocol=proto,
                    source="family_mapping",
                )

        # Step 5: Previously persisted protocol
        if persisted_protocol in SUPPORTED_PROTOCOLS:
            return ProtocolResolution(
                protocol=persisted_protocol,
                source="persisted",
            )

        # Step 6: Unresolved
        return ProtocolResolution(protocol="", source="unresolved")

    def validate_endpoint(
        self,
        model_protocol: str,
        endpoint_protocol: str,
        model_id: str,
    ) -> None:
        """Validate that the endpoint matches the model's protocol.

        Raises ValueError when the wrong endpoint is used.
        """
        if model_protocol and model_protocol != endpoint_protocol:
            raise ProtocolMismatchError(
                model_id=model_id,
                model_protocol=model_protocol,
                requested_endpoint=endpoint_protocol,
            )


class ProtocolMismatchError(Exception):
    """Raised when a model is accessed through the wrong endpoint."""

    def __init__(
        self,
        model_id: str = "",
        model_protocol: str = "",
        requested_endpoint: str = "",
    ) -> None:
        self.model_id = model_id
        self.model_protocol = model_protocol
        self.requested_endpoint = requested_endpoint
        if model_protocol == "anthropic":
            msg = (
                f"Model {model_id!r} uses the Anthropic protocol. "
                "Use /v1/messages instead of /v1/chat/completions."
            )
        else:
            msg = (
                f"Model {model_id!r} uses the OpenAI protocol. "
                "Use /v1/chat/completions instead of /v1/messages."
            )
        super().__init__(msg)
