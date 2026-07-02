"""Compression policy configuration.

Phase 4 of the cache-preserving deterministic compression roadmap
introduces observe-mode compression accounting.  Operators enable
``[compression]`` to run a cheap, side-effect-free analyzer over the
canonical request segments produced by Phase 2's segmenter.  The
analyzer records *what* it would compress and *how many tokens* it
would save, but never mutates the request body, never changes
routing, and never synthesises provider cache controls.

Phase 5 extends the config surface with ``mode = "safe"`` which
applies deterministic transforms *only* to eligible ``volatile_suffix``
segments.  ``safe`` mode never mutates stable prefixes, never mutates
cache-protected blocks, recomputes ``stable_prefix_hash`` after
compression, and fails closed (returns the uncompressed body with a
warning) on unexpected prefix hash change.

This module owns the typed config surface.  Validation rules:

- ``enabled = false`` is the safe default; no analyzer work runs
  when disabled.  ``enabled = true`` with ``mode = "observe"`` or
  ``mode = "safe"`` are the supported modes.  Unknown mode values
  fail config validation.
- ``respect_cache_boundaries = true`` suppresses every candidate
  that overlaps a protected stable-prefix segment.
- ``placement = "suffix_only"`` restricts candidates to volatile
  suffix segments; ``"after_cache_boundary"`` and ``"anywhere"``
  are reserved for later phases but accepted at config time so
  operators can express intent.
- ``min_candidate_tokens`` and ``min_savings_tokens`` must be
  non-negative.  ``max_compression_latency_ms`` is also non-negative.
- Transform toggles default to ``True`` only when compression is
  enabled.  The transforms are advisory; no analyzer runs when
  ``enabled = false``.

The ``compress_static_prefix`` flag exists for forward-compatibility
with later phases.  In Phase 4 it is documentation-only: the
analyzer never touches stable-prefix segments, so the flag has no
runtime effect.  In ``mode = "safe"`` it is rejected unless the
operator explicitly opts in via ``allow_static_prefix_override = true``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

CompressionMode = Literal["observe", "safe"]
CompressionPlacement = Literal["suffix_only", "after_cache_boundary", "anywhere"]


class CompressionTransforms(BaseModel):
    """Per-transform opt-in flags.

    Each flag defaults to ``True`` so the analyzer covers the full
    candidate surface when compression is enabled.  Operators can
    disable individual transforms to focus the analyzer on a
    subset of families.  When ``[compression] enabled = false`` the
    flags are still valid in the config; they simply have no
    runtime effect.
    """

    model_config = ConfigDict(extra="forbid")

    fold_repeated_lines: bool = Field(
        default=True,
        description=(
            "Detect runs of repeated adjacent lines in volatile-suffix "
            "segments (e.g. log noise, repeated test output)."
        ),
    )
    compact_logs: bool = Field(
        default=True,
        description=(
            "Detect large log/command-output blocks (timestamps, log "
            "levels, ANSI escapes, repeated prefixes) and estimate the "
            "token savings if first-N/last-N/error-line retention is "
            "applied."
        ),
    )
    compact_search_results: bool = Field(
        default=True,
        description=(
            "Detect ripgrep/grep/diff-shaped search output and estimate "
            "the savings if duplicate matches and excessive context are "
            "dropped."
        ),
    )
    elide_base64_blobs: bool = Field(
        default=True,
        description=(
            "Detect opaque base64 / data-URI / high-entropy blob "
            "content and estimate the savings if it is elided to a "
            "digest placeholder."
        ),
    )
    minify_machine_json: bool = Field(
        default=True,
        description=(
            "Detect large machine-generated JSON blocks where "
            "whitespace-only minification would save tokens/bytes "
            "without changing semantics."
        ),
    )
    compact_stack_traces: bool = Field(
        default=True,
        description=(
            "Detect stack-trace-shaped blocks and estimate savings "
            "from collapsing repeated frames."
        ),
    )


class CompressionConfig(BaseModel):
    """Configuration for observe-mode and safe-mode compression.

    Defaults are safe and non-mutating.  See module docstring for
    semantics.  Phase 5 ships ``mode = "observe"`` (default) and
    ``mode = "safe"``.  ``safe`` mode applies deterministic
    transforms only to eligible ``volatile_suffix`` segments; it
    never mutates stable prefixes or cache-protected blocks.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description=(
            "Master switch.  When false, the analyzer does not run "
            "and the finalizer records no compression fields.  When "
            "true and mode is 'observe', every finalized request is "
            "analyzed and a per-request summary is persisted."
        ),
    )
    mode: CompressionMode = Field(
        default="observe",
        description=(
            "Compression mode.  'observe' records opportunities "
            "without mutating the request.  'safe' applies "
            "deterministic transforms only to eligible "
            "volatile_suffix segments."
        ),
    )
    placement: CompressionPlacement = Field(
        default="suffix_only",
        description=(
            "Where candidates are allowed to land.  'suffix_only' is "
            "the only safe placement in observe mode; the other "
            "values are accepted for forward-compatibility."
        ),
    )
    respect_cache_boundaries: bool = Field(
        default=True,
        description=(
            "When true, candidates that overlap protected stable-"
            "prefix segments are suppressed and counted separately."
        ),
    )
    compress_static_prefix: bool = Field(
        default=False,
        description=(
            "Forward-compatibility flag for future phases.  Phase 4 "
            "never compresses stable prefixes regardless of this "
            "value.  Setting it to true with mode='observe' is "
            "rejected.  In mode='safe' it requires "
            "allow_static_prefix_override=true."
        ),
    )
    allow_static_prefix_override: bool = Field(
        default=False,
        description=(
            "When true, allows compress_static_prefix=true in "
            "mode='safe'.  Operators must explicitly opt in."
        ),
    )
    min_candidate_tokens: int = Field(
        default=2048,
        ge=0,
        description=(
            "Minimum estimated original-tokens for a candidate to be "
            "considered.  Smaller candidates are still scanned but "
            "are not counted as eligible."
        ),
    )
    min_savings_tokens: int = Field(
        default=1024,
        ge=0,
        description=(
            "Minimum estimated token savings for a candidate to be "
            "eligible.  Candidates with estimated savings below this "
            "threshold are recorded but suppressed."
        ),
    )
    max_compression_latency_ms: float = Field(
        default=25.0,
        ge=0.0,
        description=(
            "Per-request latency budget for the analyzer.  When the "
            "budget is exceeded the analyzer stops cleanly and the "
            "finalizer records a 'latency_budget_exceeded' warning."
        ),
    )
    transforms: CompressionTransforms = Field(
        default_factory=CompressionTransforms,
        description=(
            "Per-transform opt-in flags.  Disable individual "
            "transforms to focus the analyzer."
        ),
    )
    header_override: bool = Field(
        default=False,
        description=(
            "When true, allow per-request "
            "`x-eggpool-compression` header to override the "
            "configured mode.  Headers must be one of 'off', "
            "'observe', 'safe'."
        ),
    )
    header_cache_policy: bool = Field(
        default=True,
        description=(
            "When true, allow per-request "
            "`x-eggpool-cache-policy: preserve` header to opt out "
            "of compression for cache-equivalent flows."
        ),
    )

    @model_validator(mode="after")
    def _validate_compress_static_prefix(self) -> CompressionConfig:
        """Surface a clear error if the operator turns on a flag the
        analyzer cannot honour in the current mode.

        ``compress_static_prefix = true`` is rejected in
        ``mode = "observe"`` (Phase 4 invariant).  In ``mode = "safe"``
        it requires ``allow_static_prefix_override = true``.
        """
        if self.compress_static_prefix and self.mode == "observe":
            raise ValueError(
                "compress_static_prefix=true is not supported in mode='observe'. "
                "Disable the flag for Phase 4 or wait for a future phase that "
                "introduces a non-observe mode that honours it.",
            )
        if (
            self.compress_static_prefix
            and self.mode == "safe"
            and not self.allow_static_prefix_override
        ):
            raise ValueError(
                "compress_static_prefix=true requires "
                "allow_static_prefix_override=true in mode='safe'. "
                "Set allow_static_prefix_override=true to explicitly "
                "opt in to static prefix compression.",
            )
        return self


__all__ = [
    "CompressionConfig",
    "CompressionMode",
    "CompressionPlacement",
    "CompressionTransforms",
]
