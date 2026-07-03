"""Synthetic cache-controls configuration (Phase 9).

Phase 9 adds opt-in synthetic provider cache controls for clients and
providers whose wire format supports explicit cache boundary hints
(initially Anthropic-style ``cache_control`` annotations).  The phase
is conservative by design:

- Synthetic cache controls are disabled by default.
- Dry-run mode (the default) records what would have been
  annotated without mutating the provider-bound body.
- Apply mode mutates only supported stable-prefix blocks; volatile
  suffix and compressed content are never annotated.
- Native ``cache_control`` annotations are preserved and never
  duplicated on the same block.
- The ``QuotaFairScorer`` does NOT consume synthetic cache fields;
  routing remains load-based.

The config surface mirrors the existing ``[transcoder]`` and
``[compression]`` policy tables so operators can use the same
familiar knobs.  Per-policy overrides ride on the Phase 6
``[[compression.policies]]`` resolver so we avoid duplicating the
match-and-merge algorithm.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SyntheticCacheControlTTL = Literal["ephemeral"]

# Provider kinds that are eligible for synthetic cache controls.
# Phase 9 ships with ``anthropic`` only.  ``openai`` and others are
# reserved for future provider-specific work.
SyntheticCacheControlProviderKind = Literal["anthropic"]


class SyntheticCacheControlsConfig(BaseModel):
    """Top-level synthetic-cache-controls config.

    Lives under ``[cache]`` in the operator config.  Defaults are
    safe: synthetic controls are disabled, dry-run mode is on, and
    the minimum stable token threshold matches the documented
    Anthropic 1024-token breakpoint rule of thumb.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description=(
            "Master switch.  When false, no candidate selection runs "
            "and the finalizer records no synthetic-cache fields.  "
            "When true, the candidate selector runs but mutations "
            "are gated by ``dry_run``."
        ),
    )
    dry_run: bool = Field(
        default=True,
        description=(
            "When true, the selector records what would have been "
            "annotated without mutating the provider-bound body.  "
            "When false, the mutator applies cache_control to "
            "supported stable-prefix blocks."
        ),
    )
    provider_kinds: list[SyntheticCacheControlProviderKind] = Field(
        default_factory=lambda: ["anthropic"],
        description=(
            "Provider implementation kinds eligible for synthetic "
            "cache controls.  Phase 9 ships ``anthropic`` only."
        ),
    )
    ttl: SyntheticCacheControlTTL = Field(
        default="ephemeral",
        description=(
            "Anthropic cache TTL hint to emit.  Only ``ephemeral`` "
            "is currently supported; other values are rejected."
        ),
    )
    min_stable_tokens: int = Field(
        default=1024,
        ge=0,
        description=(
            "Minimum stable-prefix tokens required for a candidate "
            "to be considered.  Below this threshold the candidate "
            "is suppressed and counted separately."
        ),
    )
    max_breakpoints: int = Field(
        default=4,
        ge=1,
        le=64,
        description=(
            "Maximum synthetic cache breakpoints per request.  "
            "Anthropic documents a four-breakpoint limit; the cap "
            "matches Phase 3's tracker cap with a wide safety "
            "margin."
        ),
    )
    require_policy: bool = Field(
        default=True,
        description=(
            "When true, synthetic cache controls only run when a "
            "matching ``[[compression.policies]]`` override sets "
            "``synthetic_cache_*`` knobs.  When false, the global "
            "config alone controls synthetic cache behaviour."
        ),
    )
    placements: tuple[Literal["system", "tools"], ...] = Field(
        default=("system", "tools"),
        description=(
            "Stable-prefix placements eligible for synthetic "
            "annotations.  ``system`` covers top-level Anthropic "
            "``system`` blocks; ``tools`` covers ``tools[]`` "
            "definitions."
        ),
    )

    @model_validator(mode="after")
    def _validate_max_breakpoints(self) -> SyntheticCacheControlsConfig:
        """Cap the breakpoint count defensively.

        Anthropic documents four breakpoints as the practical
        maximum.  The schema already enforces ``le=64``; this
        validator surfaces a clear error if an operator pushes the
        knob past Anthropic's published limit.
        """
        if self.max_breakpoints > 4:
            raise ValueError(
                "max_breakpoints must be <= 4 to remain within "
                "Anthropic's documented cache_control breakpoint "
                "limit.",
            )
        return self


class CacheConfig(BaseModel):
    """Top-level ``[cache]`` config block.

    Houses the synthetic cache-controls toggle.  Reserved for future
    cache-related config so we do not need a fresh schema bump when
    DNS cache or similar settings move under the same umbrella.
    """

    model_config = ConfigDict(extra="forbid")

    synthetic_cache_controls: SyntheticCacheControlsConfig = Field(
        default_factory=SyntheticCacheControlsConfig,
        description=(
            "Phase 9 opt-in synthetic provider cache controls.  "
            "Disabled by default; dry-run is the default when "
            "enabled."
        ),
    )


__all__ = [
    "CacheConfig",
    "SyntheticCacheControlProviderKind",
    "SyntheticCacheControlsConfig",
    "SyntheticCacheControlTTL",
]
