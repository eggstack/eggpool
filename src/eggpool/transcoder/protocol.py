"""BodyTranscoder Protocol and factory for protocol-pair dispatch."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from eggpool.errors import ConfigError

if TYPE_CHECKING:
    from eggpool.catalog.capabilities import ThinkingCapability
    from eggpool.transcoder.context import TranscodeContext
    from eggpool.transcoder.policy import TranscoderFeatures


class BodyTranscoder(Protocol):
    """Translates a request or response body between two protocols."""

    client_protocol: str
    upstream_protocol: str

    def encode_request(
        self,
        payload: dict[str, Any],
        context: TranscodeContext,
        *,
        features: TranscoderFeatures | None = None,
        thinking_capability: ThinkingCapability | None = None,
        budget_defaults: dict[str, int] | None = None,
        budget_resolution_policy: str = "lenient",
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]: ...

    def decode_response(
        self,
        payload: dict[str, Any],
        context: TranscodeContext,
        *,
        features: TranscoderFeatures | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]: ...

    def reencode_error(
        self,
        upstream_status: int,
        upstream_payload: dict[str, Any] | None,
        context: TranscodeContext,
    ) -> tuple[int, dict[str, Any], list[dict[str, Any]]]: ...


def select_transcoder(
    *,
    client_protocol: str,
    upstream_protocol: str,
) -> BodyTranscoder | None:
    """Return the body transcoder for a protocol pair, or None when the
    pair matches and no translation is needed."""
    if client_protocol == upstream_protocol:
        return None
    if client_protocol == "openai" and upstream_protocol == "anthropic":
        from eggpool.transcoder.openai_to_anthropic import (
            OpenAIToAnthropic,
        )

        return OpenAIToAnthropic()
    if client_protocol == "anthropic" and upstream_protocol == "openai":
        from eggpool.transcoder.anthropic_to_openai import (
            AnthropicToOpenAI,
        )

        return AnthropicToOpenAI()
    raise ConfigError(
        f"Unknown protocol pair for transcoding: "
        f"{client_protocol!r} → {upstream_protocol!r}"
    )
