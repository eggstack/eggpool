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

Phase 6 adds operator-controllable policy overrides.  The global
``[compression]`` table remains the safe default.  Operators can add
``[[compression.policies]]`` rows that overlay a subset of knobs
when a request matches specific client, protocol, provider, or model
fields.  Resolution is deterministic (file order, last-match-wins on
scalar fields, merge-on-boolean for transforms), never inspects
request content, and fails closed (returns the global config plus a
warning) on any malformed override.

Phase 10 adds optional closed-loop threshold tuning.  The
``[compression.tuning]`` block enables a recommendation engine that
analyses recent compression observations and suggests bounded
adjustments to ``min_candidate_tokens``, ``min_savings_tokens``, and
``max_compression_latency_ms``.  Tuning never inspects raw prompt
content, never enables stable-prefix compression, never changes
routing, and never adds new transforms; it only adjusts the existing
conservative thresholds within operator-defined bounds.  The first
implementation milestone is ``mode = "recommend"`` (advisory) with
``mode = "apply"`` behind explicit opt-in.

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
- ``[[compression.policies]]`` entries are validated for non-empty
  unique names; all override fields are optional so absent keys do
  not reset the global default.  ``compress_static_prefix=true`` in
  an override requires the same ``allow_static_prefix_override=true``
  safety guard as the global config.
- ``[compression.tuning]`` mode must be one of ``"off"``,
  ``"recommend"``, or ``"apply"``.  Windows and cooldowns must be
  positive integers and percentage bounds must satisfy
  ``min <= max``.  Bounds are non-overlapping guards: the tuning
  engine never produces values outside
  ``[bounds.min_*, bounds.max_*]``.

The ``compress_static_prefix`` flag exists for forward-compatibility
with later phases.  In Phase 4 it is documentation-only: the
analyzer never touches stable-prefix segments, so the flag has no
runtime effect.  In ``mode = "safe"`` it is rejected unless the
operator explicitly opts in via ``allow_static_prefix_override = true``.
"""

from __future__ import annotations

from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

CompressionMode = Literal["observe", "safe"]
CompressionPlacement = Literal["suffix_only", "after_cache_boundary", "anywhere"]
CompressionProtocolMatch = Literal["openai", "anthropic"]
CompressionTuningMode = Literal["off", "recommend", "apply"]

_COMMON_TUNING_KEY_RENAMES: dict[str, str] = {
    "window_seconds": "window_requests or update_interval_s, depending on intent",
    "cooldown_seconds": "cooldown_s",
    "apply_ttl_seconds": "not supported; apply mode does not use a TTL field",
    "max_latency_warning_rate": "max_latency_budget_warning_rate",
    "target_compression_latency_ms": "max_p95_latency_ms",
}


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


# ---------------------------------------------------------------------------
# Phase 10: closed-loop threshold tuning
# ---------------------------------------------------------------------------


class CompressionTuningTargetsConfig(BaseModel):
    """Phase 10 tuning target guardrails.

    The recommendation engine compares per-policy window metrics
    against these targets and emits a reason code when a target is
    breached.  Defaults match
    ``plans/cache_compression_phase_10_closed_loop_threshold_tuning.md``
    and are intentionally conservative: latency and fallback guard
    rails come first, savings come second.
    """

    model_config = ConfigDict(extra="forbid")

    max_latency_budget_warning_rate: float = Field(
        default=0.01,
        ge=0.0,
        le=1.0,
        description=(
            "Maximum acceptable rate of latency_budget warnings "
            "(fraction of requests in window).  Above this, "
            "recommendations raise thresholds or shorten the "
            "latency budget."
        ),
    )
    max_failed_fallback_rate: float = Field(
        default=0.001,
        ge=0.0,
        le=1.0,
        description=(
            "Maximum acceptable rate of fail-closed fallback "
            "events.  Above this, recommendations raise "
            "thresholds and surface a safety warning."
        ),
    )
    min_positive_savings_rate: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum acceptable fraction of applied requests "
            "with positive token savings.  Below this, "
            "recommendations raise ``min_savings_tokens``."
        ),
    )
    min_median_savings_tokens: int = Field(
        default=512,
        ge=0,
        description=(
            "Minimum acceptable median savings tokens per applied "
            "request.  Below this, recommendations raise "
            "``min_savings_tokens``."
        ),
    )
    max_p95_latency_ms: float = Field(
        default=25.0,
        ge=0.0,
        description=(
            "Maximum acceptable p95 compression latency in "
            "milliseconds.  Above this, recommendations raise "
            "``min_candidate_tokens`` to reduce per-request work."
        ),
    )


class CompressionTuningBoundsConfig(BaseModel):
    """Phase 10 hard bounds for tunables.

    The tuning engine clamps every recommendation so the value never
    falls outside ``[min_*, max_*]``.  These bounds are the
    non-negotiable safety rail; the operator chooses the corridor.
    """

    model_config = ConfigDict(extra="forbid")

    min_candidate_tokens_min: int = Field(
        default=256,
        ge=0,
        description="Lower bound for ``min_candidate_tokens``.",
    )
    min_candidate_tokens_max: int = Field(
        default=16384,
        ge=0,
        description="Upper bound for ``min_candidate_tokens``.",
    )
    min_savings_tokens_min: int = Field(
        default=128,
        ge=0,
        description="Lower bound for ``min_savings_tokens``.",
    )
    min_savings_tokens_max: int = Field(
        default=8192,
        ge=0,
        description="Upper bound for ``min_savings_tokens``.",
    )
    max_compression_latency_ms_min: float = Field(
        default=5.0,
        ge=0.0,
        description="Lower bound for ``max_compression_latency_ms``.",
    )
    max_compression_latency_ms_max: float = Field(
        default=100.0,
        ge=0.0,
        description="Upper bound for ``max_compression_latency_ms``.",
    )

    @model_validator(mode="after")
    def _validate_min_le_max(self) -> CompressionTuningBoundsConfig:
        """Each knob's min must be <= max."""
        for lo, hi, name in (
            (
                self.min_candidate_tokens_min,
                self.min_candidate_tokens_max,
                "min_candidate_tokens",
            ),
            (
                self.min_savings_tokens_min,
                self.min_savings_tokens_max,
                "min_savings_tokens",
            ),
            (
                self.max_compression_latency_ms_min,
                self.max_compression_latency_ms_max,
                "max_compression_latency_ms",
            ),
        ):
            if lo > hi:
                raise ValueError(
                    f"compression.tuning.bounds: {name} min ({lo}) "
                    f"must be <= max ({hi}).",
                )
        return self


class CompressionTuningConfig(BaseModel):
    """Advisory threshold tuning configuration.

    Disabled by default.  The supported mode is ``"recommend"``,
    which produces advisory suggestions without changing request
    behaviour.  ``"apply"`` is accepted at config time for forward
    compatibility but is dormant today: no production code path
    registers runtime overrides, and recommendations are always
    surfaced with ``status = "recommended"``.  Tuning never touches
    routing fields, mode, enabled, or static-prefix compression,
    and never inspects raw prompt content.
    """

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _reject_common_legacy_keys(cls, value: Any) -> Any:
        """Surface a precise error for the old documented tuning keys."""
        if not isinstance(value, dict):
            return value
        raw_value = cast("dict[str, Any]", value)
        bad_keys = sorted(key for key in raw_value if key in _COMMON_TUNING_KEY_RENAMES)
        if not bad_keys:
            return raw_value
        replacements = "; ".join(
            f"{key} -> {_COMMON_TUNING_KEY_RENAMES[key]}" for key in bad_keys
        )
        raise ValueError(
            "compression.tuning contains legacy example keys; "
            f"replace them with schema-valid names ({replacements})."
        )

    enabled: bool = Field(
        default=False,
        description=(
            "Master switch for the tuning engine.  When false, the "
            "service runs no analysis, returns no recommendations, "
            "and never produces runtime overrides."
        ),
    )
    mode: CompressionTuningMode = Field(
        default="recommend",
        description=(
            "Tuning behaviour.  ``off`` runs no analysis (overrides "
            "the global ``enabled`` flag).  ``recommend`` produces "
            "advisory recommendations without changing request "
            "behaviour.  ``apply`` produces bounded runtime "
            "overrides that overlay the resolved policy."
        ),
    )
    window_requests: int = Field(
        default=500,
        gt=0,
        description=(
            "Maximum number of recent requests (per policy) used "
            "as the analysis window.  Older requests are ignored."
        ),
    )
    min_window_requests: int = Field(
        default=50,
        gt=0,
        description=(
            "Minimum number of requests required before the "
            "engine produces a non-``insufficient_data`` "
            "recommendation."
        ),
    )
    update_interval_s: int = Field(
        default=300,
        gt=0,
        description=(
            "Suggested update cadence in seconds.  Used as a hint "
            "for background tasks; the engine never relies on this "
            "for correctness."
        ),
    )
    max_adjustment_pct: float = Field(
        default=25.0,
        gt=0.0,
        le=100.0,
        description=(
            "Maximum percentage change per recommendation.  "
            "Caps the size of any single threshold step to prevent "
            "oscillation."
        ),
    )
    cooldown_s: int = Field(
        default=900,
        gt=0,
        description=(
            "Minimum seconds between two recommendations for the "
            "same policy.  Suppresses oscillating changes."
        ),
    )
    persist_recommendations: bool = Field(
        default=True,
        description=(
            "When true, the latest recommendation per policy is "
            "persisted to the ``compression_tuning_recommendations`` "
            "table so dashboards survive restart."
        ),
    )
    targets: CompressionTuningTargetsConfig = Field(
        default_factory=CompressionTuningTargetsConfig,
        description="Target guardrails the engine compares against.",
    )
    bounds: CompressionTuningBoundsConfig = Field(
        default_factory=CompressionTuningBoundsConfig,
        description="Hard bounds on every recommended threshold.",
    )

    @model_validator(mode="after")
    def _validate_windowing(self) -> CompressionTuningConfig:
        """Window + cooldown + min-window consistency.

        ``min_window_requests`` must not exceed ``window_requests``;
        otherwise the engine could never satisfy its own precondition.
        """
        if self.min_window_requests > self.window_requests:
            raise ValueError(
                "compression.tuning: min_window_requests "
                f"({self.min_window_requests}) must be <= "
                f"window_requests ({self.window_requests}).",
            )
        return self


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
    policies: list["CompressionPolicyOverride"] = Field(  # noqa: UP037
        default_factory=list["CompressionPolicyOverride"],
        description=(
            "Phase 6 operator-controllable policy overrides.  Each "
            "entry overlays a subset of the global compression knobs "
            "when a request matches the entry's match fields.  "
            "Resolution is deterministic (file order, last-match-wins "
            "on scalars, merge-on-boolean for transforms), never "
            "inspects request content, and fails closed (returns the "
            "global config plus a warning) on any malformed override.  "
            "When the list is empty, behavior is unchanged from Phase 5."
        ),
    )
    tuning: "CompressionTuningConfig" = Field(  # noqa: UP037
        default_factory=CompressionTuningConfig,
        description=(
            "Advisory threshold tuning.  Disabled by default.  When "
            "enabled, the engine analyses recent compression "
            "observations and produces bounded threshold "
            'recommendations.  ``mode = "recommend"`` is advisory '
            'and surfaces suggestions; ``mode = "apply"`` is '
            "accepted at config time but is dormant &mdash; no "
            "production code path registers runtime overrides today.  "
            "Tuning never touches routing, never enables "
            "stable-prefix compression, and never inspects raw "
            "prompt content."
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

    @model_validator(mode="after")
    def _validate_policies(self) -> CompressionConfig:
        """Phase 6 policy-table consistency.

        Names must be non-empty and unique across all entries; the
        ``compression_policy_name`` column is the audit key, so
        collisions would silently merge dashboards.  An entry with
        no match fields is allowed only when explicitly named
        ``default`` to avoid accidentally turning the override into
        a global flip.
        """
        seen: set[str] = set()
        for idx, override in enumerate(self.policies):
            if override.name in seen:
                raise ValueError(
                    f"compression.policies[{idx}]: duplicate name "
                    f"{override.name!r}; policy names must be unique.",
                )
            seen.add(override.name)
            if not override.has_any_match_field() and override.name != "default":
                raise ValueError(
                    f"compression.policies[{idx}] ({override.name!r}): "
                    "at least one match_* field must be set unless the "
                    "policy is explicitly named 'default'.",
                )
        return self


class CompressionPolicyOverride(BaseModel):
    """A single Phase 6 policy override.

    Every override field is optional so an absent key does not reset
    the global default.  ``name`` is the only required field and must
    be non-empty.  Match fields are unioned (``OR`` semantics): the
    override applies if **any** match field fires.  An override with
    no match fields applies to every request that does not match a
    later, more-specific override (file-order last-match-wins).

    ``compress_static_prefix = true`` in an override requires the
    same ``allow_static_prefix_override = true`` safety guard as the
    global config; the validator enforces it.

    Match fields support simple ``*`` prefix and suffix globbing
    (case-sensitive).  Exact strings are also accepted.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        ...,
        min_length=1,
        description=(
            "Stable, operator-chosen identifier for this override.  "
            "Persisted verbatim in the ``compression_policy_name`` "
            "column for dashboard filtering and audit."
        ),
    )
    match_clients: list[str] | None = Field(
        default=None,
        description=(
            "Authenticated client names, auth labels, or stable client "
            "IDs to match against ``CompressionPolicyContext.client_id`` "
            "and ``client_name``.  Simple ``*`` prefix/suffix globbing."
        ),
    )
    match_provider_ids: list[str] | None = Field(
        default=None,
        description=(
            "Exact provider/account IDs to match.  Only meaningful "
            "post-route; pre-route resolvers leave this as a no-op."
        ),
    )
    match_provider_kinds: list[str] | None = Field(
        default=None,
        description=(
            "Provider implementation names/kinds to match.  Only "
            "meaningful post-route; pre-route resolvers leave this "
            "as a no-op."
        ),
    )
    match_models: list[str] | None = Field(
        default=None,
        description=(
            "Resolved model IDs (after model rewrite, if any) to "
            "match.  Simple ``*`` prefix/suffix globbing."
        ),
    )
    match_requested_models: list[str] | None = Field(
        default=None,
        description=(
            "Client-supplied model IDs (before model rewrite) to "
            "match.  Simple ``*`` prefix/suffix globbing."
        ),
    )
    match_protocols: list[CompressionProtocolMatch] | None = Field(
        default=None,
        description=(
            "Source protocols to match against "
            "``CompressionPolicyContext.source_protocol``.  Use the "
            "client-side protocol before any transcoding."
        ),
    )
    match_transcoded: bool | None = Field(
        default=None,
        description=(
            "When set, only match requests where "
            "``CompressionPolicyContext.transcoded`` equals this value."
        ),
    )
    enabled: bool | None = Field(
        default=None,
        description=(
            "Override the resolved ``enabled`` flag.  ``False`` "
            "hard-disables compression for matching requests even "
            "when the global config is enabled."
        ),
    )
    mode: CompressionMode | None = Field(
        default=None,
        description=(
            "Override the resolved ``mode`` flag.  ``None`` keeps the global value."
        ),
    )
    placement: CompressionPlacement | None = Field(
        default=None,
        description=(
            "Override the resolved ``placement`` flag.  ``None`` "
            "keeps the global value."
        ),
    )
    respect_cache_boundaries: bool | None = Field(
        default=None,
        description=("Override the resolved ``respect_cache_boundaries`` flag."),
    )
    compress_static_prefix: bool | None = Field(
        default=None,
        description=(
            "Override the resolved ``compress_static_prefix`` flag.  "
            "Honoured only when the resolved mode is ``safe`` AND "
            "``allow_static_prefix_override`` is ``True``; otherwise "
            "the same validation as the global config applies."
        ),
    )
    min_candidate_tokens: int | None = Field(
        default=None,
        ge=0,
        description="Override the resolved ``min_candidate_tokens``.",
    )
    min_savings_tokens: int | None = Field(
        default=None,
        ge=0,
        description="Override the resolved ``min_savings_tokens``.",
    )
    max_compression_latency_ms: float | None = Field(
        default=None,
        ge=0.0,
        description="Override the resolved ``max_compression_latency_ms``.",
    )
    transforms: CompressionTransforms | None = Field(
        default=None,
        description=(
            "Per-transform overrides.  ``None`` keeps the global "
            "values; a non-``None`` value is merged field-by-field "
            "(``None`` inside the override keeps the global value)."
        ),
    )
    synthetic_cache_controls: bool | None = Field(
        default=None,
        description=(
            "Phase 9 override for ``[cache] synthetic_cache_controls "
            ".enabled``.  ``None`` keeps the global value."
        ),
    )
    synthetic_cache_dry_run: bool | None = Field(
        default=None,
        description=(
            "Phase 9 override for ``[cache] synthetic_cache_controls "
            ".dry_run``.  ``None`` keeps the global value."
        ),
    )
    synthetic_cache_min_stable_tokens: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Phase 9 override for ``[cache] synthetic_cache_controls "
            ".min_stable_tokens``.  ``None`` keeps the global value."
        ),
    )
    synthetic_cache_max_breakpoints: int | None = Field(
        default=None,
        ge=1,
        le=64,
        description=(
            "Phase 9 override for ``[cache] synthetic_cache_controls "
            ".max_breakpoints``.  ``None`` keeps the global value."
        ),
    )

    @model_validator(mode="after")
    def _validate_name_unique(self) -> CompressionPolicyOverride:
        """Names are surfaced by name only; an empty name has no audit value."""
        if not self.name.strip():
            raise ValueError(
                "compression.policies[].name must be a non-empty string.",
            )
        return self

    def _has_any_match_field(self) -> bool:
        """Whether the override declares at least one match_* field.

        Used by the parent ``CompressionConfig`` validator to detect
        accidental catch-all overrides that would silently flip
        every request.  Operators who genuinely want a catch-all must
        name the entry ``default``.
        """
        return any(
            getattr(self, field) is not None
            for field in (
                "match_clients",
                "match_provider_ids",
                "match_provider_kinds",
                "match_models",
                "match_requested_models",
                "match_protocols",
                "match_transcoded",
            )
        )

    def has_any_match_field(self) -> bool:
        """Public alias for :meth:`_has_any_match_field`.

        The leading-underscore form is used by the validator
        closure; this public alias keeps the cross-class call from
        tripping the private-usage rule.
        """
        return self._has_any_match_field()

    @model_validator(mode="after")
    def _validate_compress_static_prefix_override(self) -> CompressionPolicyOverride:
        """Static-prefix compression must never be silently enabled.

        The override is honoured only when paired with the global
        ``allow_static_prefix_override`` knob.  Operators who want
        this safety rail must explicitly opt in via the global
        config; per-policy opt-in alone is rejected so a single
        operator cannot accidentally enable prefix compression by
        editing one row.
        """
        if self.compress_static_prefix is True and self.mode == "observe":
            raise ValueError(
                "compress_static_prefix=true in a policy override is "
                "not supported when mode='observe'.",
            )
        if self.compress_static_prefix is True and self.mode == "safe":
            raise ValueError(
                "compress_static_prefix=true in a policy override "
                "requires the global allow_static_prefix_override=true; "
                "set [compression] allow_static_prefix_override=true "
                "and re-apply the override.",
            )
        return self


__all__ = [
    "CompressionConfig",
    "CompressionMode",
    "CompressionPlacement",
    "CompressionPolicyOverride",
    "CompressionProtocolMatch",
    "CompressionTuningBoundsConfig",
    "CompressionTuningConfig",
    "CompressionTuningMode",
    "CompressionTuningTargetsConfig",
    "CompressionTransforms",
]


# Resolve the forward reference in ``CompressionConfig.tuning`` so
# the embedded ``CompressionTuningConfig`` block is fully wired.
CompressionConfig.model_rebuild()


# Resolve the forward reference in ``CompressionConfig.policies``
# now that ``CompressionPolicyOverride`` is fully defined.
CompressionConfig.model_rebuild()
