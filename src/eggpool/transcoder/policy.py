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
            "When true, native-protocol accounts outrank transcodable ones "
            "during routing regardless of routing_priority. When false, "
            "transcodable accounts may outrank native ones if their "
            "routing_priority is higher."
        ),
    )
