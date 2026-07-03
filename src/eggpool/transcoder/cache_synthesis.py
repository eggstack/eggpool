"""Phase 9 synthetic cache-controls candidate selector and mutator.

Earlier phases preserve provider-native ``cache_control`` annotations
when they are already present on the wire and keep compression
boundary-aware.  Phase 9 adds an opt-in, dry-run-first layer that
**synthesises** cache boundary hints on the provider-bound body for
clients and providers that did not supply them.

The phase is conservative by design.  It only ever considers
protected ``stable_prefix`` segments that EggPool already knows how
to preserve exactly.  It never annotates ``volatile_suffix``
content, never annotates compressed content, and never duplicates a
provider-native ``cache_control`` annotation on the same block.

Public surface:

- :class:`SyntheticCacheCandidate` — one annotated placement.
- :class:`SyntheticCachePlan` — selector output; carries dry-run
  state, candidates, applied count, and warning codes.
- :func:`select_synthetic_cache_candidates` — pure selector.
- :func:`apply_synthetic_cache_controls` — Anthropic mutator.
- :func:`run_synthetic_cache_synthesis` — convenience wrapper that
  reads the resolved policy + segmentation and writes both the
  dry-run plan and the (optional) applied body.
- :class:`SyntheticCacheResult` — finalizer-friendly summary.

The selector is **content-private**: it never inspects raw prompt
text and only reads the segmentation metadata plus the structural
shape of the body (the same shape the segmenter already inspected).

The mutator never mutates volatile suffix content, never rewrites
the stable prefix, never changes routing, and never raises on
malformed input.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, cast

from eggpool.transcoder.cache_stability import (
    CACHE_BOUNDARY_KIND_SYNTHESIZED,
    CacheBoundaryAnnotation,
)
from eggpool.transcoder.cache_synthesis_policy import (
    CacheConfig,
    SyntheticCacheControlsConfig,
)
from eggpool.transcoder.compression.policy_resolver import (  # noqa: TC001
    ResolvedCompressionPolicy,
)
from eggpool.transcoder.context import TranscodeContext  # noqa: TC001
from eggpool.transcoder.segmentation import (
    SegmentationResult,
    SegmentKind,
    SegmentSource,
)

# Warning codes emitted by the selector and the mutator.  These are
# stable strings persisted to the ``synthetic_cache_warnings_json``
# column so dashboards can group by reason.
WARN_DISABLED = "synthetic_cache_control_disabled"
WARN_DRY_RUN = "synthetic_cache_control_dry_run"
WARN_PROVIDER_UNSUPPORTED = "synthetic_cache_control_provider_unsupported"
WARN_NO_STABLE_CANDIDATE = "synthetic_cache_control_no_stable_candidate"
WARN_BELOW_MIN_TOKENS = "synthetic_cache_control_below_min_tokens"
WARN_LIMIT_REACHED = "synthetic_cache_control_limit_reached"
WARN_SYNTHESIZED = "synthetic_cache_control_synthesized"
WARN_EXISTING_NATIVE_PRESERVED = "synthetic_cache_control_existing_native_preserved"
WARN_POLICY_REQUIRED = "synthetic_cache_control_policy_required"
WARN_NA_PAYLOAD = "synthetic_cache_control_payload_not_mapping"


# Anthropic currently documents four cache_control breakpoints as
# the practical maximum.  ``apply_synthetic_cache_controls`` honours
# ``max_breakpoints`` from the resolved config and never exceeds it.
ANTHROPIC_MAX_BREAKPOINTS: int = 4


def _path_to_display(path: tuple[str | int, ...]) -> str:
    """Convert a tuple path to a stable dot-notation display string.

    ``("system", 0, "text")`` becomes ``"system.0.text"``;
    ``("tools", 0)`` becomes ``"tools.0"``.  Integers are rendered
    as decimal strings.  Used only for display and persisted JSON;
    never for structural comparison.
    """
    return ".".join(str(p) for p in path)


@dataclass(frozen=True, slots=True)
class SyntheticCacheCandidate:
    """One candidate placement for a synthetic cache_control.

    The selector emits one entry per stable-prefix segment that the
    mutator could annotate.  Whether the annotation actually lands
    depends on ``dry_run``, the breakpoint cap, and whether the
    provider mutator supports the segment's placement.
    """

    placement: str
    """``"system"`` or ``"tools"``."""

    source_path: tuple[str | int, ...]
    """Internal canonical path the annotation would land at."""

    target_path: tuple[str | int, ...]
    """Internal canonical path after any transcoding; same as
    ``source_path`` when no transcoding occurred."""

    estimated_tokens: int | None
    """Cheap token estimate for the annotated block; ``None`` when
    segmentation did not compute one."""

    reason: str
    """Stable reason code: ``stable_prefix_candidate``,
    ``tool_schema_candidate``, ``system_candidate``."""

    policy_name: str
    """Name of the policy whose match triggered the candidate (or
    ``"<global>"`` when no policy override matched)."""

    policy_source: str
    """Audit string identifying the source (global vs. policy)."""

    ttl: str
    """Effective TTL the mutator will apply (e.g. ``"ephemeral"``)."""


@dataclass(frozen=True, slots=True)
class SyntheticCachePlan:
    """Selector output for one request.

    ``status`` is a stable string persisted to the
    ``synthetic_cache_status`` column:

    - ``disabled`` — synthetic cache controls are off (global or
      policy).
    - ``dry_run`` — selector ran in dry-run mode; ``applied_count``
      is always 0.
    - ``applied`` — at least one candidate was mutated.
    - ``no_candidates`` — selector ran but found no eligible
      placement.
    - ``policy_required`` — ``require_policy`` blocked the run.
    - ``provider_unsupported`` — the resolved target protocol/kind
      is not in the configured provider kinds.
    """

    status: str
    dry_run: bool
    candidates: tuple[SyntheticCacheCandidate, ...]
    applied_count: int
    warnings: tuple[str, ...]
    policy_name: str
    policy_source: str
    effective_ttl: str = "ephemeral"

    @property
    def candidate_count(self) -> int:
        """Number of candidates the selector surfaced."""
        return len(self.candidates)

    def as_dict(self) -> dict[str, Any]:
        """Compact dict for the persistence layer.

        No raw prompt text, no body content.  Only the structural
        counts and warning codes are persisted.
        """
        return {
            "status": self.status,
            "dry_run": self.dry_run,
            "candidate_count": len(self.candidates),
            "applied_count": self.applied_count,
            "warning_count": len(self.warnings),
            "warnings": list(self.warnings),
            "policy_name": self.policy_name,
            "policy_source": self.policy_source,
            "effective_ttl": self.effective_ttl,
            "placements": sorted({c.placement for c in self.candidates}),
            "reasons": sorted({c.reason for c in self.candidates}),
            "candidate_source_paths": sorted(
                _path_to_display(c.source_path) for c in self.candidates
            ),
            "candidate_target_paths": sorted(
                _path_to_display(c.target_path) for c in self.candidates
            ),
        }


@dataclass(slots=True)
class SyntheticCacheResult:
    """Finalizer-friendly summary produced by
    :func:`run_synthetic_cache_synthesis`.

    The fields are duck-typed by ``FinalizationData`` so the
    finalizer can ``getattr`` them without importing this module.
    """

    plan: SyntheticCachePlan
    transformed_payload: dict[str, Any] | None
    """Mutated payload, or ``None`` when dry-run / disabled / failed."""

    cache_boundary_entries: tuple[CacheBoundaryAnnotation, ...]
    """Synthetic boundary annotations recorded against the tracker."""

    summary_json: str
    """JSON string with the structured plan summary for persistence."""

    @property
    def status(self) -> str:
        return self.plan.status

    @property
    def dry_run(self) -> bool:
        return self.plan.dry_run

    @property
    def candidate_count(self) -> int:
        return len(self.plan.candidates)

    @property
    def applied_count(self) -> int:
        return self.plan.applied_count

    @property
    def warning_count(self) -> int:
        return len(self.plan.warnings)

    @property
    def warnings(self) -> tuple[str, ...]:
        return self.plan.warnings

    @property
    def policy_name(self) -> str:
        return self.plan.policy_name

    @property
    def policy_source(self) -> str:
        return self.plan.policy_source


# Mapping from the prefixed override keys surfaced by the Phase 6
# resolver to the unprefixed field names on
# :class:`SyntheticCacheControlsConfig`.  Phase 9 operators write
# ``synthetic_cache_*`` overrides in ``[[compression.policies]]``
# (matching the existing field name pattern on
# :class:`CompressionPolicyOverride`) but the effective config uses
# the unprefixed names so the schema validator stays the single
# source of truth.
_SYNTHETIC_OVERRIDE_KEY_MAP: dict[str, str] = {
    "synthetic_cache_controls": "enabled",
    "synthetic_cache_dry_run": "dry_run",
    "synthetic_cache_min_stable_tokens": "min_stable_tokens",
    "synthetic_cache_max_breakpoints": "max_breakpoints",
}


def _merge_synthetic_cache_config(
    cache_config: CacheConfig,
    synthetic_overrides: dict[str, Any] | None,
) -> SyntheticCacheControlsConfig:
    """Build the effective Phase 9 synthetic-cache config.

    Starts from ``cache_config.synthetic_cache_controls`` and
    overlays any non-``None`` Phase 9 override fields supplied by
    the compression policy resolver.  Returns a defensive copy so
    callers cannot mutate the global config by accident.
    """
    base = cache_config.synthetic_cache_controls.model_copy(deep=True)
    if not synthetic_overrides:
        return base
    dump: Any = base.model_dump()
    if not isinstance(dump, dict):
        return base
    base_dict: dict[str, Any] = cast("dict[str, Any]", dump)
    for key, value in synthetic_overrides.items():
        if value is None:
            continue
        target_key = _SYNTHETIC_OVERRIDE_KEY_MAP.get(key, key)
        if target_key in base_dict:
            base_dict[target_key] = value
    return SyntheticCacheControlsConfig.model_validate(base_dict)


def _candidate_policy_metadata(
    resolved_policy: ResolvedCompressionPolicy | None,
) -> tuple[str, str]:
    """Return ``(policy_name, policy_source)`` for audit columns."""
    if resolved_policy is None:
        return ("<global>", "global")
    return (resolved_policy.name, resolved_policy.source)


def _select_candidates_for_anthropic(
    segmentation: SegmentationResult,
    config: SyntheticCacheControlsConfig,
    policy_name: str,
    policy_source: str,
) -> tuple[tuple[SyntheticCacheCandidate, ...], tuple[str, ...]]:
    """Walk the stable-prefix segments and pick Anthropic candidates.

    Only protected ``stable_prefix`` segments whose
    ``SegmentSource`` is ``SYSTEM``, ``DEVELOPER``, or
    ``TOOL_SCHEMA`` are eligible.  Segments already carrying a
    native ``cache_control`` annotation are recorded as preserved
    and skipped — never duplicated.  The selector respects
    ``min_stable_tokens`` and ``max_breakpoints``.
    """
    candidates: list[SyntheticCacheCandidate] = []
    warnings: list[str] = []
    placements = set(config.placements)
    ttl = config.ttl

    native_preserved = 0
    for segment in segmentation.stable_prefix_segments:
        if segment.kind is not SegmentKind.STABLE_PREFIX:
            continue
        if not segment.protected:
            continue
        # Skip pure cache_control metadata segments; they have no
        # leaf to annotate.
        if segment.source is SegmentSource.CACHE_CONTROL:
            continue
        if segment.source is SegmentSource.SYSTEM and "system" not in placements:
            continue
        if segment.source is SegmentSource.DEVELOPER and "system" not in placements:
            continue
        if segment.source is SegmentSource.TOOL_SCHEMA and "tools" not in placements:
            continue
        # Map source -> reason + placement.
        if (
            segment.source is SegmentSource.SYSTEM
            or segment.source is SegmentSource.DEVELOPER
        ):
            reason = "system_candidate"
            placement = "system"
        elif segment.source is SegmentSource.TOOL_SCHEMA:
            reason = "tool_schema_candidate"
            placement = "tools"
        else:
            reason = "stable_prefix_candidate"
            placement = "system"
        tokens = segment.estimated_tokens
        if tokens is not None and tokens < config.min_stable_tokens:
            warnings.append(WARN_BELOW_MIN_TOKENS)
            continue
        path = tuple(segment.content_path)
        candidates.append(
            SyntheticCacheCandidate(
                placement=placement,
                source_path=path,
                target_path=path,
                estimated_tokens=tokens,
                reason=reason,
                policy_name=policy_name,
                policy_source=policy_source,
                ttl=ttl,
            )
        )

    if not candidates:
        # Native preservation is informational; surface it once.
        if native_preserved:
            warnings.append(WARN_EXISTING_NATIVE_PRESERVED)
        return ((), tuple(warnings))

    # Apply breakpoint cap; prefer the last stable-prefix block
    # before semi-stable/volatile content (selector naturally orders
    # by segment index, so we keep tail-end candidates when the cap
    # binds).
    if len(candidates) > config.max_breakpoints:
        warnings.append(WARN_LIMIT_REACHED)
        candidates = candidates[-config.max_breakpoints :]

    if native_preserved:
        warnings.append(WARN_EXISTING_NATIVE_PRESERVED)
    return (tuple(candidates), tuple(warnings))


def _existing_native_cache_controls(
    payload: Any,
) -> set[tuple[str | int, ...]]:
    """Return container paths that already carry a native
    ``cache_control`` annotation.

    Paths are **container** paths — the path TO the dict that holds
    ``cache_control``, not including the ``.cache_control`` leaf.
    For example ``("system", 0)`` for ``system[0].cache_control``.

    Walks the same surfaces the segmenter walked; never raises on
    malformed input.
    """
    found: set[tuple[str | int, ...]] = set()
    if not isinstance(payload, dict):
        return found
    payload_dict = cast("dict[str, Any]", payload)
    system = payload_dict.get("system")
    if isinstance(system, list):
        for index, block in enumerate(cast("list[Any]", system)):
            if isinstance(block, dict) and cast("dict[str, Any]", block).get(
                "cache_control"
            ):
                found.add(("system", index))
    tools = payload_dict.get("tools")
    if isinstance(tools, list):
        for index, tool in enumerate(cast("list[Any]", tools)):
            if isinstance(tool, dict) and cast("dict[str, Any]", tool).get(
                "cache_control"
            ):
                found.add(("tools", index))
    messages = payload_dict.get("messages")
    if isinstance(messages, list):
        for message_index, message in enumerate(cast("list[Any]", messages)):
            if not isinstance(message, dict):
                continue
            message_dict = cast("dict[str, Any]", message)
            content = message_dict.get("content")
            if isinstance(content, list):
                for block_index, block in enumerate(cast("list[Any]", content)):
                    if isinstance(block, dict) and cast("dict[str, Any]", block).get(
                        "cache_control"
                    ):
                        found.add(("messages", message_index, "content", block_index))
    return found


def _structural_cache_diff(
    original: dict[str, Any],
    mutated: dict[str, Any],
) -> dict[str, Any]:
    """Compare two payloads and report paths of changes.

    Returns a dict with:
    - ``added_paths``: paths present in ``mutated`` but not in ``original``.
    - ``removed_paths``: paths present in ``original`` but not in ``mutated``.
    - ``changed_paths``: paths whose values differ.

    Paths are represented as ``list[str | int]`` using dot-style
    string keys and integer indices.  The mutator is supposed to
    **only** add ``cache_control`` keys at candidate container
    paths.  Any other change is a safety failure.

    Use :func:`_validate_synthetic_cache_diff` to confirm the diff
    is consistent with a candidate set; this helper only reports
    raw structural differences.
    """
    added: list[list[str | int]] = []
    removed: list[list[str | int]] = []
    changed: list[list[str | int]] = []

    def _walk(
        o: Any,
        m: Any,
        path: list[str | int],
    ) -> None:
        if type(o) is not type(m):
            changed.append(list(path))
            return
        if isinstance(o, dict):
            o_dict = cast("dict[str, Any]", o)
            m_dict = cast("dict[str, Any]", m)
            all_keys = set(o_dict) | set(m_dict)
            for key in sorted(all_keys, key=str):
                child = list(path) + [key]
                if key not in o_dict:
                    added.append(child)
                elif key not in m_dict:
                    removed.append(child)
                else:
                    _walk(o_dict[key], m_dict[key], child)
        elif isinstance(o, list):
            o_list = cast("list[Any]", o)
            m_list = cast("list[Any]", m)
            max_len = max(len(o_list), len(m_list))
            for i in range(max_len):
                child = list(path) + [i]
                if i >= len(o_list):
                    added.append(child)
                elif i >= len(m_list):
                    removed.append(child)
                else:
                    _walk(o_list[i], m_list[i], child)
        else:
            if o != m:
                changed.append(list(path))

    _walk(original, mutated, [])
    return {"added_paths": added, "removed_paths": removed, "changed_paths": changed}


def _validate_synthetic_cache_diff(
    diff: dict[str, Any],
    candidates: tuple[SyntheticCacheCandidate, ...],
) -> bool:
    """Return ``True`` when the diff is consistent with a candidate set.

    A mutator is allowed to add ``cache_control`` only at the
    container paths implied by the candidate set.  Any added path
    whose last component is ``"cache_control"`` but whose container
    is not in the candidate set is a safety failure — the mutator
    must never annotate containers that the selector did not pick.
    Any non-``cache_control`` addition, removal, or change is also
    a safety failure.
    """
    allowed_added_paths: set[tuple[str | int, ...]] = {
        tuple(list(_container_path_for_candidate(c.target_path)) + ["cache_control"])
        for c in candidates
    }
    for added_path in diff.get("added_paths", []):
        last = added_path[-1] if added_path else None
        if last != "cache_control":
            return False
        if tuple(added_path) not in allowed_added_paths:
            return False
    return not diff.get("removed_paths") and not diff.get("changed_paths")


def select_synthetic_cache_candidates(
    segmentation: SegmentationResult | None,
    payload: Any,
    *,
    cache_config: CacheConfig,
    target_protocol: str,
    target_provider_kind: str | None,
    resolved_policy: ResolvedCompressionPolicy | None,
) -> SyntheticCachePlan:
    """Run the candidate selector against the segmentation summary.

    Returns a :class:`SyntheticCachePlan` describing whether
    synthetic cache controls would have applied.  ``dry_run`` is
    preserved on the plan; the mutator is invoked separately via
    :func:`apply_synthetic_cache_controls`.
    """
    policy_name, policy_source = _candidate_policy_metadata(resolved_policy)
    effective = _merge_synthetic_cache_config(
        cache_config,
        resolved_policy.synthetic_cache_overrides
        if resolved_policy is not None
        else None,
    )

    if not effective.enabled:
        return SyntheticCachePlan(
            status="disabled",
            dry_run=effective.dry_run,
            candidates=(),
            applied_count=0,
            warnings=(WARN_DISABLED,),
            policy_name=policy_name,
            policy_source=policy_source,
            effective_ttl=effective.ttl,
        )

    if effective.require_policy and policy_source == "global":
        return SyntheticCachePlan(
            status="policy_required",
            dry_run=effective.dry_run,
            candidates=(),
            applied_count=0,
            warnings=(WARN_POLICY_REQUIRED,),
            policy_name=policy_name,
            policy_source=policy_source,
            effective_ttl=effective.ttl,
        )

    supported_provider_kinds: set[str] = set(effective.provider_kinds)
    if (
        target_provider_kind is not None
        and supported_provider_kinds
        and target_provider_kind not in supported_provider_kinds
    ):
        return SyntheticCachePlan(
            status="provider_unsupported",
            dry_run=effective.dry_run,
            candidates=(),
            applied_count=0,
            warnings=(WARN_PROVIDER_UNSUPPORTED,),
            policy_name=policy_name,
            policy_source=policy_source,
            effective_ttl=effective.ttl,
        )

    if not isinstance(payload, dict):
        return SyntheticCachePlan(
            status="disabled",
            dry_run=effective.dry_run,
            candidates=(),
            applied_count=0,
            warnings=(WARN_NA_PAYLOAD,),
            policy_name=policy_name,
            policy_source=policy_source,
            effective_ttl=effective.ttl,
        )

    payload_dict = cast("dict[str, Any]", payload)

    if segmentation is None:
        return SyntheticCachePlan(
            status="no_candidates",
            dry_run=effective.dry_run,
            candidates=(),
            applied_count=0,
            warnings=(WARN_NO_STABLE_CANDIDATE,),
            policy_name=policy_name,
            policy_source=policy_source,
            effective_ttl=effective.ttl,
        )

    if target_protocol != "anthropic":
        return SyntheticCachePlan(
            status="provider_unsupported",
            dry_run=effective.dry_run,
            candidates=(),
            applied_count=0,
            warnings=(WARN_PROVIDER_UNSUPPORTED,),
            policy_name=policy_name,
            policy_source=policy_source,
            effective_ttl=effective.ttl,
        )

    existing_native = _existing_native_cache_controls(payload_dict)
    candidates, selector_warnings = _select_candidates_for_anthropic(
        segmentation,
        effective,
        policy_name,
        policy_source,
    )
    if existing_native:
        selector_warnings = tuple(selector_warnings) + (WARN_EXISTING_NATIVE_PRESERVED,)
    if not candidates:
        warnings = list(selector_warnings) + [WARN_NO_STABLE_CANDIDATE]
        return SyntheticCachePlan(
            status="no_candidates",
            dry_run=effective.dry_run,
            candidates=(),
            applied_count=0,
            warnings=tuple(warnings),
            policy_name=policy_name,
            policy_source=policy_source,
            effective_ttl=effective.ttl,
        )
    if effective.dry_run:
        warnings = list(selector_warnings) + [WARN_DRY_RUN]
        return SyntheticCachePlan(
            status="dry_run",
            dry_run=True,
            candidates=candidates,
            applied_count=0,
            warnings=tuple(warnings),
            policy_name=policy_name,
            policy_source=policy_source,
            effective_ttl=effective.ttl,
        )
    warnings = list(selector_warnings) + [WARN_SYNTHESIZED]
    return SyntheticCachePlan(
        status="applied",
        dry_run=False,
        candidates=candidates,
        applied_count=len(candidates),
        warnings=tuple(warnings),
        policy_name=policy_name,
        policy_source=policy_source,
        effective_ttl=effective.ttl,
    )


def _resolve_tuple_path(payload: dict[str, Any], path: tuple[str | int, ...]) -> Any:
    """Resolve a tuple path inside ``payload``.

    Returns ``None`` when the path cannot be resolved.
    """
    current: Any = payload
    for part in path:
        if isinstance(current, dict):
            if not isinstance(part, str):
                return None
            current = cast("dict[str, Any]", current).get(part)
        elif isinstance(current, list):
            if not isinstance(part, int):
                return None
            try:
                current = cast("list[Any]", current)[part]
            except IndexError:
                return None
        else:
            return None
        if current is None:
            return None
    return current


def _walk_to_dict_container(
    payload: dict[str, Any],
    path: tuple[str | int, ...],
) -> Any:
    """Resolve ``path`` against ``payload``; if the leaf is not a
    dict, walk back one path component at a time until one is.

    Stable-prefix segments sometimes point at string leaves
    (``("system", 0, "text")``) where the provider cache_control
    belongs on the parent block (``("system", 0)``).  When the
    segmenter has already pointed at the dict (``("tools", 0)``),
    the first lookup succeeds.  Returns ``None`` when no dict
    ancestor exists, so the caller can silently skip the candidate.
    """
    while path:
        candidate: Any = _resolve_tuple_path(payload, path)
        if isinstance(candidate, dict):
            return cast("dict[str, Any]", candidate)
        path = path[:-1]
    return None


def _container_path_for_candidate(
    path: tuple[str | int, ...],
) -> tuple[str | int, ...]:
    """Derive the container path from a candidate's target path.

    If the path already points at a dict container (last element is
    an int or a known container key), return it unchanged.  If the
    path points at a text leaf (e.g. ``("system", 0, "text")``),
    strip the last component to reach the owning container.
    """
    if not path:
        return path
    last = path[-1]
    if isinstance(last, int):
        return path
    if last == "text" and len(path) >= 2:
        return path[:-1]
    return path


def _synthesize_anthropic_payload(
    payload: dict[str, Any],
    candidates: tuple[SyntheticCacheCandidate, ...],
    ttl: str,
    existing_native: set[tuple[str | int, ...]],
) -> tuple[dict[str, Any], int]:
    """Apply the candidate annotations to a deep copy of ``payload``.

    Returns the (possibly mutated) copy and the count of
    successfully-applied annotations.  Native cache_control
    annotations are preserved and never duplicated.  Failures
    silently skip the candidate (the selector already filtered out
    under-token candidates).

    The ``target_path`` of a candidate points at the **content leaf**
    the segmenter recorded (e.g. ``("system", 0, "text")`` for an
    Anthropic ``system`` block whose ``text`` field is the string
    leaf).  The mutator walks back up the path until it lands on a
    dict so ``cache_control`` can be attached as a sibling.  ``tools``
    candidates already point at the dict container
    (``("tools", 0)``) so they resolve on the first try.
    """
    mutated = copy.deepcopy(payload)
    applied = 0
    cache_control_block = {"type": ttl}
    for candidate in candidates:
        container_path = _container_path_for_candidate(candidate.target_path)
        if container_path in existing_native:
            continue
        container = _walk_to_dict_container(mutated, container_path)
        if not isinstance(container, dict):
            continue
        cast("dict[str, Any]", container)["cache_control"] = dict(cache_control_block)
        applied += 1
    return mutated, applied


def apply_synthetic_cache_controls(
    payload: dict[str, Any],
    plan: SyntheticCachePlan,
) -> tuple[dict[str, Any], int, tuple[CacheBoundaryAnnotation, ...]]:
    """Mutate ``payload`` to add the synthetic cache_control hints.

    Always returns a fresh dict (the original ``payload`` is never
    mutated).  The third tuple element is the list of synthetic
    boundary annotations the caller should record against the
    ``CacheBoundaryTracker``.

    When the plan is in dry-run mode or otherwise non-applied, this
    function returns the input ``payload`` unchanged and an empty
    annotation list.
    """
    if plan.status != "applied" or plan.applied_count == 0:
        return payload, 0, ()
    existing_native = _existing_native_cache_controls(payload)
    mutated, applied_count = _synthesize_anthropic_payload(
        payload,
        plan.candidates,
        ttl=plan.effective_ttl,
        existing_native=existing_native,
    )
    annotations: list[CacheBoundaryAnnotation] = []
    for candidate in plan.candidates:
        container_path = _container_path_for_candidate(candidate.target_path)
        if container_path in existing_native:
            continue
        annotations.append(
            CacheBoundaryAnnotation(
                kind=CACHE_BOUNDARY_KIND_SYNTHESIZED,
                source_protocol="anthropic",
                target_protocol="anthropic",
                source_path=_path_to_display(candidate.source_path),
                target_path=_path_to_display(container_path) + ".cache_control",
                cache_control_type=plan.effective_ttl,
            )
        )
    return mutated, applied_count, tuple(annotations)


def run_synthetic_cache_synthesis(
    payload: Any,
    *,
    segmentation: SegmentationResult | None,
    cache_config: CacheConfig,
    target_protocol: str,
    target_provider_kind: str | None,
    resolved_policy: ResolvedCompressionPolicy | None,
    transcode_context: TranscodeContext | None = None,
) -> SyntheticCacheResult:
    """One-shot selector + optional mutator.

    Returns a :class:`SyntheticCacheResult` whose
    ``transformed_payload`` is ``None`` when no mutation occurred
    (dry-run, disabled, no candidates).  Records synthetic
    annotations on the provided ``transcode_context`` when one is
    supplied so the Phase 3 ``CacheBoundaryTracker`` and the
    cache-stability summary pick them up automatically.
    """
    plan = select_synthetic_cache_candidates(
        segmentation,
        payload,
        cache_config=cache_config,
        target_protocol=target_protocol,
        target_provider_kind=target_provider_kind,
        resolved_policy=resolved_policy,
    )
    transformed: dict[str, Any] | None = None
    annotations: tuple[CacheBoundaryAnnotation, ...] = ()
    if plan.status == "applied" and isinstance(payload, dict):
        mutated, applied_count, annotations = apply_synthetic_cache_controls(
            cast("dict[str, Any]", payload), plan
        )
        transformed = mutated
        # Reconcile applied_count in case the mutator skipped some
        # candidates (e.g. native duplicate or path resolution
        # failure).
        if applied_count != plan.applied_count:
            plan = SyntheticCachePlan(
                status=plan.status,
                dry_run=plan.dry_run,
                candidates=plan.candidates,
                applied_count=applied_count,
                warnings=plan.warnings,
                policy_name=plan.policy_name,
                policy_source=plan.policy_source,
                effective_ttl=plan.effective_ttl,
            )
        if transcode_context is not None:
            tracker = transcode_context.cache_boundary_tracker
            for annotation in annotations:
                tracker.record(annotation)
    import json

    summary_json = json.dumps(plan.as_dict(), ensure_ascii=False, sort_keys=True)
    return SyntheticCacheResult(
        plan=plan,
        transformed_payload=transformed,
        cache_boundary_entries=annotations,
        summary_json=summary_json,
    )


__all__ = [
    "ANTHROPIC_MAX_BREAKPOINTS",
    "SyntheticCacheCandidate",
    "SyntheticCachePlan",
    "SyntheticCacheResult",
    "WARN_BELOW_MIN_TOKENS",
    "WARN_DISABLED",
    "WARN_DRY_RUN",
    "WARN_EXISTING_NATIVE_PRESERVED",
    "WARN_LIMIT_REACHED",
    "WARN_NA_PAYLOAD",
    "WARN_NO_STABLE_CANDIDATE",
    "WARN_POLICY_REQUIRED",
    "WARN_PROVIDER_UNSUPPORTED",
    "WARN_SYNTHESIZED",
    "_path_to_display",
    "_structural_cache_diff",
    "_validate_synthetic_cache_diff",
    "apply_synthetic_cache_controls",
    "run_synthetic_cache_synthesis",
    "select_synthetic_cache_candidates",
]
