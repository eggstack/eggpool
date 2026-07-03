"""Reusable helpers for Phase 11 cache/compression fixture replay tests.

The harness is intentionally thin: it loads JSON fixtures, expands
optional compact repeat specs, and runs the high-risk Phase 2/5/9/10
pipelines against the loaded request payload. Tests can also import
the helpers for in-process use without going through a fixture file.

Public surface:

- :func:`load_fixture` -- read a JSON fixture from disk
- :func:`expand_repeats` -- apply a compact repeat spec to a payload
- :func:`safe_policy` / :func:`observe_policy` -- deterministic policies
- :func:`run_full_replay` -- execute every pipeline step, return bundle
- :func:`run_provider_bound_synthetic_replay` -- explicit provider-bound
  helper for transcode fixtures (Phase 12 polish pass)
- :class:`ReplayBundle` -- the deterministic structural summary
- :func:`default_fixture_root` -- repo-relative fixture root path

Replay shape semantics
----------------------

The harness exposes two replay shapes:

- **client-shape replay**: segmentation + compression + synthetic cache
  are all run against the original client payload using the
  ``client_protocol``. This is the default for ``run_full_replay`` and
  matches Phase 5 compression, which is intentionally client-bound.
- **provider-bound replay**: transcode is run first when
  ``client_protocol != target_protocol``; segmentation, synthetic
  cache, and cache-stability observation are then run against the
  **provider-bound** body using the ``target_protocol``. This matches
  the production Phase 9 path (``_apply_synthetic_cache_controls``)
  which executes post-route on the upstream body.

``run_full_replay`` records the shape it used in
``ReplayBundle.synthetic_cache_shape``.  When ``client_protocol !=
target_protocol`` and a ``synthetic_cache`` config is supplied, the
bundle additionally records provider-bound segmentation/synthesis
fields alongside the client-shape fields. Callers that need the full
provider-bound lifecycle should use :func:`run_provider_bound_synthetic_replay`
which always uses the post-transcode body for synthetic cache.

The harness never logs raw request content on failure. Failure log lines
emit the fixture name, the expected vs observed status, and the
structural path tuples -- never the underlying prompt text.
"""

from __future__ import annotations

import copy as _copy
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from eggpool.transcoder.cache_synthesis import (
    run_synthetic_cache_synthesis,
)
from eggpool.transcoder.cache_synthesis_policy import (
    CacheConfig,
    SyntheticCacheControlsConfig,
)
from eggpool.transcoder.compression import (
    CompressionConfig,
    apply_safe_compression,
)
from eggpool.transcoder.compression.policy import (
    CompressionTransforms,
)
from eggpool.transcoder.context import TranscodeContext
from eggpool.transcoder.protocol import (
    select_transcoder,
)
from eggpool.transcoder.segmentation import (
    SegmentationResult,
    SegmentKind,
    segment_request,
    stable_prefix_content_hash,
)

if TYPE_CHECKING:
    from eggpool.transcoder.compression.policy_resolver import (
        ResolvedCompressionPolicy,
    )

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_FIXTURE_ROOT: Path = _REPO_ROOT / "tests" / "fixtures" / "cache_compression"

DEFAULT_PROVIDER_KIND = "anthropic"

SyntheticCacheShape = Literal[
    "disabled",
    "client_bound",
    "provider_bound",
    "provider_bound_unavailable",
]


def default_fixture_root() -> Path:
    """Return the absolute path of the cache_compression fixture root."""
    return _FIXTURE_ROOT


# ---------------------------------------------------------------------------
# Fixture loading
# ---------------------------------------------------------------------------


def load_fixture(name: str, *, root: Path | None = None) -> dict[str, Any]:
    """Load one fixture JSON by relative filename.

    The name can be either a relative path under the fixture root
    (``openai/simple_stable_prefix``) or a bare stem
    (``simple_stable_prefix``).
    """
    base = root if root is not None else _FIXTURE_ROOT
    candidates = [
        base / name,
        base / f"{name}.json",
    ]
    if "/" not in name:
        for category_dir in sorted(base.iterdir()):
            if not category_dir.is_dir():
                continue
            candidates.append(category_dir / f"{name}.json")
    for path in candidates:
        if path.is_file():
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                raise ValueError(
                    f"Fixture {name} must be a JSON object, got {type(data).__name__}"
                )
            return data
    msg = f"Fixture {name!r} not found under {base}."
    msg += f" Tried: {[str(p) for p in candidates]}"
    raise FileNotFoundError(msg)


def iter_fixtures(
    category: str | None = None, *, root: Path | None = None
) -> list[dict[str, Any]]:
    """Return every fixture JSON under the (optional) category directory."""
    base = root if root is not None else _FIXTURE_ROOT
    fixtures: list[dict[str, Any]] = []
    target = base / category if category else base
    if not target.exists():
        return fixtures
    for path in sorted(target.rglob("*.json")):
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            fixtures.append(data)
    return fixtures


# ---------------------------------------------------------------------------
# Compact repeat expansion
# ---------------------------------------------------------------------------


def expand_repeats(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Apply a ``repeats`` overlay onto a payload tree.

    The overlay is a dict where each top-level key targets a JSON path
    (dot-separated) on the payload.  Each entry may contain:

    - ``fields``: dict of fields to set on every repeated element
    - ``repeat``: int -- number of copies to materialise
    - ``append_after_role``: optional role name under
      ``messages`` after which to insert the repeats

    The overlay mutates a deep copy so the caller's payload is never
    affected.
    """
    base = _copy.deepcopy(dict(payload))
    repeats = base.pop("repeats", None)
    if not isinstance(repeats, Mapping):
        return base
    for path, spec in repeats.items():
        if not isinstance(spec, Mapping):
            continue
        count = int(spec.get("repeat", 0))
        if count <= 0:
            continue
        base = _apply_repeat(base, path, count, spec)
    return base


def _apply_repeat(
    payload: dict[str, Any], path: str, count: int, spec: Mapping[str, Any]
) -> dict[str, Any]:
    """Insert ``count`` repeated entries into a list at ``path``.

    Paths use dot notation with bracketed indices, e.g. ``messages``.
    The simplest supported shape is ``messages`` -- append after a chosen
    role -- or any plain list at a nested key.
    """
    parts = path.split(".")
    cursor = payload
    for part in parts:
        if not isinstance(cursor, dict):
            return payload
        cursor = cursor.setdefault(part, [])
    if not isinstance(cursor, list):
        return payload
    fields = spec.get("fields") or {}
    anchor_role = spec.get("append_after_role")
    if anchor_role is not None:
        target_index = len(cursor)
        for idx, entry in enumerate(cursor):
            if isinstance(entry, dict) and entry.get("role") == anchor_role:
                target_index = idx + 1
        for _ in range(count):
            cursor.insert(target_index, dict(fields))
            target_index += 1
    else:
        for _ in range(count):
            cursor.append(dict(fields))
    return payload


# ---------------------------------------------------------------------------
# Replay bundle data class
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReplayBundle:
    """The deterministic structural summary returned by :func:`run_full_replay`.

    No raw prompt text is captured; only segment paths, hashes, statuses,
    transform counts, and marker counts are stored.  Tests compare these
    fields to fixture expectations.

    Replay shape semantics
    ----------------------

    The :attr:`synthetic_cache_shape` field records which replay shape
    the synthetic-cache step used:

    - ``disabled``: no ``synthetic_cache`` config supplied
    - ``client_bound``: synthetic cache ran against the client-shape
      payload using ``client_protocol`` (used when
      ``client_protocol == target_protocol``, or for transcode
      fixtures when no transcode was needed)
    - ``provider_bound``: synthetic cache ran against the provider-bound
      payload using ``target_protocol`` (transcode ran first)
    - ``provider_bound_unavailable``: transcode produced no provider
      payload so the synthetic-cache step had to fall back to client-shape

    When ``synthetic_cache_shape == "provider_bound"``,
    :attr:`provider_bound_segmentation_status`,
    :attr:`provider_bound_synthetic_cache_status`, and
    :attr:`provider_bound_synthetic_cache_candidate_count` carry
    the provider-bound observations. The
    ``synthetic_cache_status`` /
    ``synthetic_cache_candidate_count`` fields still reflect the
    client-shape pass for backwards compatibility.
    """

    fixture_name: str
    client_protocol: str
    target_protocol: str
    segmentation_status: str
    segment_counts_by_kind: dict[str, int]
    stable_prefix_content_hash: str
    pre_compression_hash: str
    post_compression_hash: str
    compression_applied: bool
    compression_failed_fallback: bool
    transforms_by_reason: dict[str, int]
    synthetic_cache_shape: SyntheticCacheShape = "disabled"
    synthetic_cache_status: str = "disabled"
    synthetic_cache_dry_run: bool = True
    synthetic_cache_candidate_count: int = 0
    synthetic_cache_applied_count: int = 0
    provider_bound_segmentation_status: str = ""
    provider_bound_synthetic_cache_status: str = ""
    provider_bound_synthetic_cache_candidate_count: int = 0
    cache_boundary_counts: dict[str, int] = field(default_factory=dict)
    transcoded_warnings: tuple[str, ...] = ()
    raw_segmentation: SegmentationResult | None = None


# ---------------------------------------------------------------------------
# Policy builders
# ---------------------------------------------------------------------------


def _transforms(**overrides: bool) -> CompressionTransforms:
    defaults: dict[str, bool] = {
        "fold_repeated_lines": True,
        "compact_logs": True,
        "compact_search_results": True,
        "elide_base64_blobs": True,
        "minify_machine_json": True,
        "compact_stack_traces": True,
    }
    defaults.update(overrides)
    return CompressionTransforms(**defaults)


def safe_policy(
    *, min_candidate_tokens: int = 0, min_savings_tokens: int = 0, **overrides: Any
) -> CompressionConfig:
    """Return a permissive safe-mode :class:`CompressionConfig` for tests."""
    top_overrides: dict[str, Any] = dict(
        enabled=True,
        mode="safe",
        placement="suffix_only",
        respect_cache_boundaries=True,
        compress_static_prefix=False,
        min_candidate_tokens=min_candidate_tokens,
        min_savings_tokens=min_savings_tokens,
        max_compression_latency_ms=200.0,
    )
    top_overrides.update(overrides)
    return CompressionConfig(
        **top_overrides,
        transforms=_transforms(),
    )


def observe_policy(**overrides: Any) -> CompressionConfig:
    """Return a permissive observe-mode :class:`CompressionConfig`."""
    top_overrides: dict[str, Any] = dict(
        enabled=True,
        mode="observe",
        placement="suffix_only",
        respect_cache_boundaries=True,
        compress_static_prefix=False,
        min_candidate_tokens=0,
        min_savings_tokens=0,
        max_compression_latency_ms=200.0,
    )
    top_overrides.update(overrides)
    return CompressionConfig(
        **top_overrides,
        transforms=_transforms(),
    )


def disabled_policy() -> CompressionConfig:
    """Return a compression-disabled :class:`CompressionConfig`."""
    return CompressionConfig(
        enabled=False,
        mode="observe",
        transforms=_transforms(),
    )


def synthetic_cache_config(
    *, enabled: bool, dry_run: bool, require_policy: bool = False, **overrides: Any
) -> CacheConfig:
    """Build a :class:`CacheConfig` with controllable synthetic-cache knobs."""
    return CacheConfig(
        synthetic_cache_controls=SyntheticCacheControlsConfig(
            enabled=enabled,
            dry_run=dry_run,
            require_policy=require_policy,
            **overrides,
        ),
    )


# ---------------------------------------------------------------------------
# Replay entry points
# ---------------------------------------------------------------------------


def run_segmentation(
    payload: Mapping[str, Any],
    *,
    protocol: str,
) -> SegmentationResult:
    """Run Phase 2 :func:`segment_request` over a payload."""
    return segment_request(payload, protocol=protocol)


def run_compression(
    payload: Mapping[str, Any],
    segmentation: SegmentationResult,
    *,
    policy: CompressionConfig,
    text_hints: Mapping[str, str] | None = None,
) -> Any:
    """Run Phase 5 :func:`apply_safe_compression` over a payload.

    Returns the :class:`CompressionResult` so tests can inspect
    ``stable_prefix_preserved``, ``transforms_by_reason``, etc.
    """
    return apply_safe_compression(
        payload,
        segmentation,
        policy=policy,
        text_hints=text_hints,
    )


def run_transcode(
    payload: Mapping[str, Any],
    *,
    client_protocol: str,
    target_protocol: str,
) -> tuple[TranscodeContext, dict[str, Any] | None, tuple[dict[str, Any], ...]]:
    """Run a body transcoder and return the (context, transformed body, warnings).

    Returns ``(context, transformed_body_or_none, warnings_tuple)``.
    Raises :class:`ValueError` if the protocols are identical (caller
    must short-circuit before calling).
    """
    transcoder = select_transcoder(
        client_protocol=client_protocol,
        upstream_protocol=target_protocol,
    )
    if transcoder is None:
        return (
            TranscodeContext(
                request_id="replay",
                client_protocol=client_protocol,
                upstream_protocol=target_protocol,
            ),
            None,
            (),
        )
    ctx = TranscodeContext(
        request_id="replay",
        client_protocol=client_protocol,
        upstream_protocol=target_protocol,
    )
    transformed, warnings = transcoder.encode_request(dict(payload), ctx)
    return ctx, transformed, tuple(warnings)


def run_synthetic(
    payload: Mapping[str, Any],
    segmentation: SegmentationResult,
    *,
    cache_config: CacheConfig,
    target_protocol: str,
    target_provider_kind: str = DEFAULT_PROVIDER_KIND,
    resolved_policy: ResolvedCompressionPolicy | None = None,
) -> Any:
    """Run Phase 9 synthetic-cache selection+application."""
    return run_synthetic_cache_synthesis(
        payload,
        segmentation=segmentation,
        cache_config=cache_config,
        target_protocol=target_protocol,
        target_provider_kind=target_provider_kind,
        resolved_policy=resolved_policy,
    )


# ---------------------------------------------------------------------------
# Full replay
# ---------------------------------------------------------------------------


def _transcode_request(
    request: Mapping[str, Any],
    *,
    client_protocol: str,
    target_protocol: str,
) -> tuple[TranscodeContext, dict[str, Any] | None, tuple[dict[str, Any], ...]]:
    """Wrap :func:`run_transcode` for transcode-fixture replay.

    Returns the (context, provider_bound_body_or_none, warnings) tuple.
    """
    return run_transcode(
        request,
        client_protocol=client_protocol,
        target_protocol=target_protocol,
    )


def _run_synthetic_with_shape(
    *,
    shape: SyntheticCacheShape,
    payload: Mapping[str, Any],
    target_protocol: str,
    cache_config: CacheConfig,
    provider_kind: str = DEFAULT_PROVIDER_KIND,
) -> tuple[Any, SegmentationResult | None]:
    """Run synthetic cache and return (result, segmentation_used).

    ``segmentation_used`` is the segmentation that drove the synthetic
    cache decision -- it can be the client-shape segmentation or the
    provider-bound one.
    """
    segmentation = segment_request(payload, protocol=target_protocol)
    result = run_synthetic_cache_synthesis(
        payload,
        segmentation=segmentation,
        cache_config=cache_config,
        target_protocol=target_protocol,
        target_provider_kind=provider_kind,
        resolved_policy=None,
    )
    return result, segmentation


def run_full_replay(
    fixture: Mapping[str, Any],
    *,
    compression_policy: CompressionConfig | None = None,
    text_hints: Mapping[str, str] | None = None,
    synthetic_cache: CacheConfig | None = None,
) -> ReplayBundle:
    """Run segmentation + compression + synthesis + transcoder and bundle the result.

    Replay shape semantics
    ----------------------

    Compression is always run in **client-shape** -- Phase 5 is by design
    client-bound in production too.  Synthetic cache follows this rule:

    - If ``client_protocol == target_protocol`` (or no transcode is
      needed): synthetic cache runs on the client-shape payload; the
      bundle records ``synthetic_cache_shape="client_bound"``.
    - If ``client_protocol != target_protocol``: transcode runs first
      and synthetic cache runs on the provider-bound payload using
      ``target_protocol`` -- the bundle records
      ``synthetic_cache_shape="provider_bound"``. The provider-bound
      segmentation status and candidate count are recorded on the
      bundle as ``provider_bound_*`` fields. The client-shape
      ``synthetic_cache_status`` / ``synthetic_cache_candidate_count``
      fields still reflect the *client-shape* pass for backwards
      compatibility.
    - If transcode produces no provider-bound body, synthetic cache
      cannot run provider-bound; the bundle records
      ``synthetic_cache_shape="provider_bound_unavailable"`` and the
      synthetic-cache fields stay at ``disabled`` defaults.

    Callers that need explicit provider-bound semantics for transcode
    fixtures should prefer :func:`run_provider_bound_synthetic_replay`.

    Compression is run in safe mode unless ``compression_policy`` is
    supplied.  Synthesis is skipped unless ``synthetic_cache`` is supplied.
    """
    name = str(fixture.get("name", "<unknown>"))
    client_protocol = str(fixture.get("client_protocol", "openai"))
    target_protocol = str(fixture.get("target_protocol", client_protocol))
    expanded = expand_repeats(fixture)
    request = expanded.get("request") if "request" in expanded else expanded
    if not isinstance(request, dict):
        raise ValueError(
            f"Fixture {name!r} must declare a 'request' object after expansion."
        )

    segmentation = segment_request(request, protocol=client_protocol)
    pre_hash = stable_prefix_content_hash(request, segmentation)

    comp_policy = (
        compression_policy if compression_policy is not None else safe_policy()
    )

    compression_result = apply_safe_compression(
        request,
        segmentation,
        policy=comp_policy,
        text_hints=text_hints,
    )
    post_hash = stable_prefix_content_hash(
        compression_result.transformed_payload, segmentation
    )

    (
        synthetic_shape,
        synthetic_status,
        synthetic_dry_run,
        synthetic_candidate_count,
        synthetic_applied_count,
        provider_segmentation_status,
        provider_synthetic_status,
        provider_candidate_count,
        cache_boundary_counts,
        transcoded_warnings,
    ) = _replay_synthetic_and_transcode(
        request=request,
        client_protocol=client_protocol,
        target_protocol=target_protocol,
        synthetic_cache=synthetic_cache,
    )

    return ReplayBundle(
        fixture_name=name,
        client_protocol=client_protocol,
        target_protocol=target_protocol,
        segmentation_status=str(segmentation.status.value),
        segment_counts_by_kind={
            kind.value: count
            for kind, count in segmentation.segment_count_by_kind.items()
        },
        stable_prefix_content_hash=str(segmentation.stable_prefix_hash),
        pre_compression_hash=pre_hash,
        post_compression_hash=post_hash,
        compression_applied=bool(compression_result.applied),
        compression_failed_fallback=bool(compression_result.failed_fallback),
        transforms_by_reason={
            str(k): int(v) for k, v in compression_result.transforms_by_reason.items()
        },
        synthetic_cache_shape=synthetic_shape,
        synthetic_cache_status=synthetic_status,
        synthetic_cache_dry_run=synthetic_dry_run,
        synthetic_cache_candidate_count=synthetic_candidate_count,
        synthetic_cache_applied_count=synthetic_applied_count,
        provider_bound_segmentation_status=provider_segmentation_status,
        provider_bound_synthetic_cache_status=provider_synthetic_status,
        provider_bound_synthetic_cache_candidate_count=provider_candidate_count,
        cache_boundary_counts=cache_boundary_counts,
        transcoded_warnings=transcoded_warnings,
        raw_segmentation=segmentation,
    )


def _replay_synthetic_and_transcode(
    *,
    request: Mapping[str, Any],
    client_protocol: str,
    target_protocol: str,
    synthetic_cache: CacheConfig | None,
) -> tuple[
    SyntheticCacheShape,
    str,
    bool,
    int,
    int,
    str,
    str,
    int,
    dict[str, int],
    tuple[str, ...],
]:
    """Drive the (transcode + synthetic cache) slice of ``run_full_replay``.

    Returns the (shape, status, dry_run, candidate_count, applied_count,
    provider_segmentation_status, provider_synthetic_status,
    provider_candidate_count, cache_boundary_counts, transcoded_warnings)
    tuple used to populate a :class:`ReplayBundle`.

    The helper isolates the branching used to pick between client-shape
    and provider-bound synthetic-cache replay. ``run_full_replay``
    delegates here so this logic does not have to be inlined in the
    public entrypoint.
    """
    cache_boundary_counts: dict[str, int] = {}
    transcoded_warnings: tuple[str, ...] = ()

    synthetic_shape: SyntheticCacheShape = "disabled"
    synthetic_status = "disabled"
    synthetic_dry_run = True
    synthetic_candidate_count = 0
    synthetic_applied_count = 0
    provider_segmentation_status = ""
    provider_synthetic_status = ""
    provider_candidate_count = 0

    needs_transcode = client_protocol != target_protocol
    has_synthetic = synthetic_cache is not None

    if needs_transcode:
        # always evaluate transcode warnings for transcode fixtures
        _, provider_body, warnings = _transcode_request(
            request,
            client_protocol=client_protocol,
            target_protocol=target_protocol,
        )
        transcoded_warnings = tuple(
            str(w.get("kind")) for w in warnings if isinstance(w, dict)
        )

    if not has_synthetic:
        return (
            synthetic_shape,
            synthetic_status,
            synthetic_dry_run,
            synthetic_candidate_count,
            synthetic_applied_count,
            provider_segmentation_status,
            provider_synthetic_status,
            provider_candidate_count,
            cache_boundary_counts,
            transcoded_warnings,
        )

    if not needs_transcode:
        # client-shape synthetic cache for same-protocol fixtures
        result, _ = _run_synthetic_with_shape(
            shape="client_bound",
            payload=request,
            target_protocol=client_protocol,
            cache_config=synthetic_cache,
        )
        plan = result.plan
        synthetic_shape = "client_bound"
        synthetic_status = plan.status
        synthetic_dry_run = plan.dry_run
        synthetic_candidate_count = len(plan.candidates)
        synthetic_applied_count = plan.applied_count
        return (
            synthetic_shape,
            synthetic_status,
            synthetic_dry_run,
            synthetic_candidate_count,
            synthetic_applied_count,
            provider_segmentation_status,
            provider_synthetic_status,
            provider_candidate_count,
            cache_boundary_counts,
            transcoded_warnings,
        )

    # transcode present -- run synthetic cache on the provider-bound body
    if provider_body is None:
        # transcode produced no body; record unavailable sentinel and skip
        synthetic_shape = "provider_bound_unavailable"
        return (
            synthetic_shape,
            synthetic_status,
            synthetic_dry_run,
            synthetic_candidate_count,
            synthetic_applied_count,
            provider_segmentation_status,
            provider_synthetic_status,
            provider_candidate_count,
            cache_boundary_counts,
            transcoded_warnings,
        )

    provider_segmentation = segment_request(provider_body, protocol=target_protocol)
    provider_segmentation_status = str(provider_segmentation.status.value)

    result = run_synthetic_cache_synthesis(
        provider_body,
        segmentation=provider_segmentation,
        cache_config=synthetic_cache,
        target_protocol=target_protocol,
        target_provider_kind=DEFAULT_PROVIDER_KIND,
        resolved_policy=None,
    )
    plan = result.plan
    synthetic_shape = "provider_bound"
    synthetic_status = plan.status
    synthetic_dry_run = plan.dry_run
    synthetic_candidate_count = len(plan.candidates)
    synthetic_applied_count = plan.applied_count
    provider_synthetic_status = plan.status
    provider_candidate_count = len(plan.candidates)
    return (
        synthetic_shape,
        synthetic_status,
        synthetic_dry_run,
        synthetic_candidate_count,
        synthetic_applied_count,
        provider_segmentation_status,
        provider_synthetic_status,
        provider_candidate_count,
        cache_boundary_counts,
        transcoded_warnings,
    )


def run_provider_bound_synthetic_replay(
    fixture: Mapping[str, Any],
    *,
    compression_policy: CompressionConfig | None = None,
    text_hints: Mapping[str, str] | None = None,
    synthetic_cache: CacheConfig | None = None,
) -> ReplayBundle:
    """Run the full replay with explicit provider-bound semantics.

    This helper mirrors production Phase 9: it transcribes the client
    payload into the target provider protocol first, then runs
    segmentation, compression, and synthetic cache against the
    provider-bound body.

    When ``client_protocol == target_protocol`` (no transcode needed),
    this behaves the same as :func:`run_full_replay`.

    The returned :class:`ReplayBundle` always carries
    ``synthetic_cache_shape="provider_bound"`` (or
    ``"provider_bound_unavailable"`` if transcode produced no body).

    Use this helper for tests that need to assert against the
    provider-bound segmentation/protocol path -- for example, the
    OpenAI-to-Anthropic transcoding fixture.
    """
    name = str(fixture.get("name", "<unknown>"))
    client_protocol = str(fixture.get("client_protocol", "openai"))
    target_protocol = str(fixture.get("target_protocol", client_protocol))
    expanded = expand_repeats(fixture)
    request = expanded.get("request") if "request" in expanded else expanded
    if not isinstance(request, dict):
        raise ValueError(
            f"Fixture {name!r} must declare a 'request' object after expansion."
        )

    needs_transcode = client_protocol != target_protocol
    provider_body: dict[str, Any] | None = None
    transcoded_warnings: tuple[str, ...] = ()
    cache_boundary_counts: dict[str, int] = {}
    if needs_transcode:
        _, provider_body, warnings = _transcode_request(
            request,
            client_protocol=client_protocol,
            target_protocol=target_protocol,
        )
        transcoded_warnings = tuple(
            str(w.get("kind")) for w in warnings if isinstance(w, dict)
        )

    payload_for_pipeline: dict[str, Any] = (
        provider_body if provider_body is not None else dict(request)
    )
    pipeline_protocol = target_protocol

    segmentation = segment_request(payload_for_pipeline, protocol=pipeline_protocol)
    pre_hash = stable_prefix_content_hash(payload_for_pipeline, segmentation)

    comp_policy = (
        compression_policy if compression_policy is not None else safe_policy()
    )
    compression_result = apply_safe_compression(
        payload_for_pipeline,
        segmentation,
        policy=comp_policy,
        text_hints=text_hints,
    )
    post_hash = stable_prefix_content_hash(
        compression_result.transformed_payload, segmentation
    )

    synthetic_shape: SyntheticCacheShape = "disabled"
    synthetic_status = "disabled"
    synthetic_dry_run = True
    synthetic_candidate_count = 0
    synthetic_applied_count = 0
    provider_segmentation_status = ""
    if synthetic_cache is not None:
        if needs_transcode and provider_body is None:
            synthetic_shape = "provider_bound_unavailable"
        else:
            result = run_synthetic_cache_synthesis(
                payload_for_pipeline,
                segmentation=segmentation,
                cache_config=synthetic_cache,
                target_protocol=pipeline_protocol,
                target_provider_kind=DEFAULT_PROVIDER_KIND,
                resolved_policy=None,
            )
            plan = result.plan
            synthetic_shape = "provider_bound"
            synthetic_status = plan.status
            synthetic_dry_run = plan.dry_run
            synthetic_candidate_count = len(plan.candidates)
            synthetic_applied_count = plan.applied_count
        provider_segmentation_status = str(segmentation.status.value)

    return ReplayBundle(
        fixture_name=name,
        client_protocol=client_protocol,
        target_protocol=target_protocol,
        segmentation_status=str(segmentation.status.value),
        segment_counts_by_kind={
            kind.value: count
            for kind, count in segmentation.segment_count_by_kind.items()
        },
        stable_prefix_content_hash=str(segmentation.stable_prefix_hash),
        pre_compression_hash=pre_hash,
        post_compression_hash=post_hash,
        compression_applied=bool(compression_result.applied),
        compression_failed_fallback=bool(compression_result.failed_fallback),
        transforms_by_reason={
            str(k): int(v) for k, v in compression_result.transforms_by_reason.items()
        },
        synthetic_cache_shape=synthetic_shape,
        synthetic_cache_status=synthetic_status,
        synthetic_cache_dry_run=synthetic_dry_run,
        synthetic_cache_candidate_count=synthetic_candidate_count,
        synthetic_cache_applied_count=synthetic_applied_count,
        provider_bound_segmentation_status=provider_segmentation_status,
        provider_bound_synthetic_cache_status=synthetic_status,
        provider_bound_synthetic_cache_candidate_count=synthetic_candidate_count,
        cache_boundary_counts=cache_boundary_counts,
        transcoded_warnings=transcoded_warnings,
        raw_segmentation=segmentation,
    )


# ---------------------------------------------------------------------------
# Tree inspection helpers
# ---------------------------------------------------------------------------


def collect_segment_strings(
    segmentation: SegmentationResult, *, payload: Any
) -> dict[str, list[str]]:
    """Return a map from :class:`SegmentKind` value to the string leaves under it.

    Used by the harness to assert that a fixture's stable prefix and
    volatile suffix are recognised by the segmenter.
    """
    from eggpool.transcoder.segmentation import resolve_text_path

    grouped: dict[str, list[str]] = {
        SegmentKind.STABLE_PREFIX.value: [],
        SegmentKind.SEMI_STABLE_CONTEXT.value: [],
        SegmentKind.VOLATILE_SUFFIX.value: [],
    }
    for segment in segmentation.all_segments():
        leaf = resolve_text_path(payload, segment.content_path)
        if leaf is not None:
            grouped[segment.kind.value].append(leaf)
    return grouped


def path_keys(payload: Mapping[str, Any]) -> set[tuple[Any, ...]]:
    """Return the set of dot-paths that carry a ``cache_control`` key."""
    paths: set[tuple[Any, ...]] = set()

    def walk(node: Any, prefix: tuple[Any, ...]) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                current = prefix + (key,)
                if key == "cache_control":
                    paths.add(prefix)
                walk(value, current)
        elif isinstance(node, list):
            for idx, value in enumerate(node):
                walk(value, prefix + (idx,))

    walk(payload, ())
    return paths


__all__ = [
    "DEFAULT_PROVIDER_KIND",
    "ReplayBundle",
    "SyntheticCacheShape",
    "collect_segment_strings",
    "default_fixture_root",
    "disabled_policy",
    "expand_repeats",
    "iter_fixtures",
    "load_fixture",
    "observe_policy",
    "path_keys",
    "run_compression",
    "run_full_replay",
    "run_provider_bound_synthetic_replay",
    "run_segmentation",
    "run_synthetic",
    "run_transcode",
    "safe_policy",
    "synthetic_cache_config",
]
