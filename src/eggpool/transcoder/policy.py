"""TranscoderPolicy — configuration surface for protocol transcoding.

Transcoding is **on by default**. EggPool's data plane normalises every
client request to the appropriate upstream wire format automatically:
an OpenAI client posting to ``/v1/chat/completions`` reaches Anthropic
upstreams (and vice versa) without any operator configuration.

The ``enabled`` flag is preserved as a deprecated escape hatch for
operators who need to disable translation — e.g. for diagnosis or to
pin behaviour while debugging routing. Setting ``enabled = false``
restores the pre-default behaviour where every request must match its
upstream protocol exactly; this option will be removed in a future
release.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CapabilityPolicy(BaseModel):
    """Policy for capability-aware routing decisions.

    Controls how EggPool routes requests that explicitly ask for
    thinking/reasoning support when the candidate model's capability
    status is not ``"supported"``.
    """

    model_config = ConfigDict(extra="forbid")

    unsupported_thinking: Literal["reject", "warn_drop", "route_best_effort"] = Field(
        default="reject",
        description=(
            "How to handle candidates whose thinking status is 'unsupported'. "
            "'reject' excludes them from routing. 'warn_drop' routes but "
            "logs a warning. 'route_best_effort' ignores the status entirely."
        ),
    )
    unknown_thinking: Literal["reject", "allow_with_warning", "route_best_effort"] = (
        Field(
            default="reject",
            description=(
                "How to handle candidates whose thinking status is 'unknown'. "
                "'reject' excludes them. 'allow_with_warning' routes but logs. "
                "'route_best_effort' ignores the status."
            ),
        )
    )
    mixed_collapsed_thinking: Literal["filter", "reject", "allow"] = Field(
        default="filter",
        description=(
            "How to handle collapsed-model entries with mixed provider "
            "thinking support. 'filter' narrows to supported providers. "
            "'reject' excludes the model. 'allow' ignores the status."
        ),
    )


class TranscoderFeatures(BaseModel):
    """Per-feature opt-in flags for Phase 6 sub-phases.

    Each flag is **off** by default.  Operators enable individual
    features under ``[transcoder.features]``.  When a feature is off
    the v1 behaviour (drop with warning) prevails for any input that
    exercises that feature.
    """

    model_config = ConfigDict(extra="forbid")

    tools: bool = Field(
        default=False,
        description=(
            "Bidirectional tool calling translation. When enabled, OpenAI "
            "tools/tool_choice are translated to Anthropic format and vice "
            "versa, including streaming tool-call deltas."
        ),
    )
    vision: bool = Field(
        default=False,
        description=(
            "Image / document content parts. When enabled, OpenAI image_url "
            "content is translated to Anthropic image/document blocks and "
            "vice versa."
        ),
    )
    thinking: bool = Field(
        default=False,
        description=(
            "Extended thinking / reasoning blocks. When enabled, Anthropic "
            "thinking blocks are translated to OpenAI reasoning_content and "
            "vice versa."
        ),
    )
    structured_outputs: bool = Field(
        default=False,
        description=(
            "OpenAI response_format / json_schema coercion. When enabled, "
            "json_schema response format is translated to system-prompt "
            "coercion for Anthropic upstreams."
        ),
    )
    anthropic_primitives: bool = Field(
        default=False,
        description=(
            "Anthropic-only primitives (top_k, cache_control, etc.). When "
            "enabled, explicit handling replaces drop-with-warning for "
            "supported primitives."
        ),
    )


class TranscoderPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=True,
        description=(
            "DEPRECATED ESCAPE HATCH. Defaults to true; EggPool automatically "
            "translates between OpenAI Chat Completions and Anthropic "
            "Messages when the client protocol does not match the routed "
            "upstream protocol. Set to false to disable translation and "
            "require protocol-exact routing (legacy behaviour). This option "
            "will be removed in a future release."
        ),
    )

    loss_policy: Literal["warn", "reject"] = Field(
        default="warn",
        description=(
            "How to handle loss-of-information during transcoding. 'warn' "
            "emits a structured log per request. 'reject' returns a 400 "
            "when request-body translation would drop or alter fields before "
            "upstream dispatch."
        ),
    )

    prefer_native: bool = Field(
        default=True,
        description=(
            "When true, native-protocol accounts win ties against "
            "transcodable ones inside the selected routing_priority tier. "
            "When false, transcodable accounts can win whenever their quota "
            "score ranks ahead."
        ),
    )

    features: TranscoderFeatures = Field(
        default_factory=TranscoderFeatures,
        description=(
            "Per-feature opt-in flags for Phase 6 sub-phases. Each flag "
            "defaults to false; enable individual features as needed."
        ),
    )

    capability_policy: CapabilityPolicy = Field(
        default_factory=CapabilityPolicy,
        description=(
            "Capability-aware routing policy. Controls how requests with "
            "explicit thinking/reasoning controls are routed when the "
            "candidate model's capability status is not 'supported'."
        ),
    )
