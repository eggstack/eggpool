"""TranscoderPolicy — configuration surface for protocol transcoding."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TranscoderPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description=(
            "When false (default), every request must match its upstream "
            "protocol exactly. When true, requests are transcoded when the "
            "selected account does not natively support the client protocol."
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
