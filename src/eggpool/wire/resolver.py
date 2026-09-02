"""Bounded, process-owned runtime wire-profile selection state.

This module owns preference learning and negotiation admission only.  It does
not inspect HTTP status codes or response bodies; callers must provide an
explicitly authorized transition before suppressing a candidate or starting a
negotiation flight.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
import time
from collections import OrderedDict, deque
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, cast

from eggpool.wire.registry import resolve_provider_wire_profiles

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from eggpool.models.config import ModelWirePreference, ProviderConfig
    from eggpool.wire.registry import WireHint
    from eggpool.wire.types import WireProfile, WireSurfaceName

logger = logging.getLogger(__name__)

WireCacheKey = tuple[str, str, str]
NegotiationRole = Literal["leader", "follower", "throttled"]


@dataclass(frozen=True, slots=True)
class CandidateRejection:
    """Bounded negative-cache state for one candidate surface."""

    last_deterministic_rejection_at: float
    suppress_until: float
    last_rejection_class: str


def _new_candidate_rejections() -> dict[str, CandidateRejection]:
    """Create an explicitly typed bounded rejection map."""
    return {}


@dataclass(slots=True)
class WirePreferenceEntry:
    """Learned provider/model preference without credentials or response data."""

    preferred_surface: WireSurfaceName
    last_success_monotonic: float
    last_success_epoch: float
    source: str
    confidence_timestamp: float
    candidate_rejections: dict[str, CandidateRejection] = field(
        default_factory=_new_candidate_rejections
    )


@dataclass(frozen=True, slots=True)
class WireResolution:
    """Ordered candidates and metadata for one request-path resolution."""

    provider_id: str
    model_id: str
    candidate_fingerprint: str
    candidates: tuple[WireProfile, ...]
    selected_source: str
    fixed: bool

    @property
    def preferred(self) -> WireProfile | None:
        """Return the first candidate, if any."""
        return self.candidates[0] if self.candidates else None


@dataclass(frozen=True, slots=True)
class WireNegotiationResult:
    """Bounded result shared by a negotiation leader and its followers."""

    provider_id: str
    model_id: str
    accepted_surface: WireSurfaceName | None
    result: Literal["accepted", "rejected", "rate_limited", "throttled"]


class _ProviderGate:
    """A dynamically resizable async semaphore for abnormal dispatches."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._active = 0
        self._limit = 1
        self._waiters: deque[asyncio.Future[None]] = deque()

    async def acquire(self, limit: int) -> None:
        limit = max(1, limit)
        waiter: asyncio.Future[None] | None = None
        async with self._lock:
            self._limit = limit
            if self._active < self._limit and not self._waiters:
                self._active += 1
                return
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
        try:
            await waiter
        except asyncio.CancelledError:
            async with self._lock:
                with suppress(ValueError):
                    self._waiters.remove(waiter)
            raise

    async def release(self) -> None:
        async with self._lock:
            self._active = max(0, self._active - 1)
            while self._waiters and self._active < self._limit:
                waiter = self._waiters.popleft()
                if waiter.cancelled():
                    continue
                self._active += 1
                waiter.set_result(None)
                break


@dataclass(slots=True)
class NegotiationHandle:
    """Leader/follower handle for one provider/model negotiation flight."""

    _resolver: WireProfileResolver
    provider_id: str
    model_id: str
    candidate_fingerprint: str
    role: NegotiationRole
    future: asyncio.Future[WireNegotiationResult]
    _max_concurrent_per_provider: int
    _min_interval_s: float
    _gate: _ProviderGate | None = None
    _entered: bool = False
    _finished: bool = False

    @property
    def is_leader(self) -> bool:
        """Whether this handle owns candidate dispatch."""
        return self.role == "leader"

    async def __aenter__(self) -> NegotiationHandle:
        if self.is_leader and not self._entered:
            self._entered = True
            gate = self._resolver.provider_gate(self.provider_id)
            self._gate = gate
            try:
                await gate.acquire(self._max_concurrent_per_provider)
            except asyncio.CancelledError:
                await self.finish(result="rejected")
                raise
            self._resolver.mark_negotiation_started(
                self.provider_id, self._min_interval_s
            )
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.is_leader and not self._finished:
            await self.finish(result="rejected")

    async def accept(self, surface: WireSurfaceName) -> WireNegotiationResult:
        """Record an accepted candidate and release the single-flight guard."""
        self._resolver.record_success(
            self.provider_id,
            self.model_id,
            self.candidate_fingerprint,
            surface,
            source="learned_runtime",
        )
        return await self.finish(surface=surface, result="accepted")

    async def reject(
        self,
        surface: WireSurfaceName,
        *,
        rejection_class: str,
        cooldown_s: float,
    ) -> None:
        """Suppress one candidate after the caller authorized rejection."""
        self._resolver.record_deterministic_rejection(
            self.provider_id,
            self.model_id,
            self.candidate_fingerprint,
            surface,
            rejection_class=rejection_class,
            cooldown_s=cooldown_s,
        )

    async def rate_limited(
        self, *, retry_after_s: float | None = None
    ) -> WireNegotiationResult:
        """Stop candidate enumeration and delay future negotiation attempts."""
        delay = 60.0 if retry_after_s is None else max(0.0, min(retry_after_s, 1800.0))
        self._resolver.delay_provider_negotiation(self.provider_id, delay)
        return await self.finish(result="rate_limited")

    async def finish(
        self,
        *,
        surface: WireSurfaceName | None = None,
        result: Literal["accepted", "rejected", "rate_limited"] = "rejected",
    ) -> WireNegotiationResult:
        """Publish one bounded decision and release provider capacity."""
        if not self._finished:
            self._finished = True
            decision = WireNegotiationResult(
                provider_id=self.provider_id,
                model_id=self.model_id,
                accepted_surface=surface,
                result=result,
            )
            self._resolver.finish_flight(self, decision)
            if self._gate is not None:
                await self._gate.release()
        return await self.future

    async def wait_for_acceptance(self) -> WireNegotiationResult:
        """Wait for the leader's wire decision, never its model response."""
        return await self.future


@dataclass(slots=True)
class _NegotiationFlight:
    future: asyncio.Future[WireNegotiationResult]
    candidate_fingerprint: str


class WireProfileResolver:
    """Process-owned bounded resolver and negotiation governor.

    The resolver is intentionally request-reactive.  ``resolve`` is a cheap
    in-memory operation, ``record_success`` is synchronous, and negotiation
    admission is the only async coordination path.  It never starts a task
    or performs network I/O on its own.
    """

    def __init__(self, *, cache_max_entries: int = 2048) -> None:
        self._cache_max_entries = max(1, cache_max_entries)
        self._max_concurrent_per_provider = 1
        self._min_negotiation_interval_s = 1.0
        self._rejection_cooldown_s = 300.0
        self._learned_preference_ttl_s = 86400.0
        self._entries: OrderedDict[WireCacheKey, WirePreferenceEntry] = OrderedDict()
        self._flights: dict[tuple[str, str], _NegotiationFlight] = {}
        self._provider_gates: dict[str, _ProviderGate] = {}
        self._next_negotiation_allowed_at: dict[str, float] = {}
        self._state_lock = threading.RLock()
        self._metrics: dict[str, int] = {}

    @staticmethod
    def candidate_fingerprint(
        provider: ProviderConfig,
        profiles: Iterable[WireProfile],
        *,
        model_preference: ModelWirePreference | None = None,
        allowed_surfaces: Iterable[WireSurfaceName] | None = None,
        metadata_surface: WireSurfaceName | None = None,
        bundled_hint: WireHint | WireSurfaceName | None = None,
    ) -> str:
        """Hash candidate structure and request constraints safely."""
        profile_rows: list[dict[str, object]] = []
        for profile in profiles:
            profile_rows.append(
                {
                    "surface": profile.surface,
                    "request_codec": profile.request_codec,
                    "response_codec": profile.response_codec,
                    "stream_codec": profile.stream_codec,
                    "path": profile.path_template,
                    "stream_path": profile.stream_path_template,
                    "auth": {
                        "mode": profile.auth.mode,
                        "header": profile.auth.header,
                        "scheme": profile.auth.scheme,
                        "additional": [
                            {
                                "mode": entry.mode,
                                "header": entry.header,
                                "scheme": entry.scheme,
                            }
                            for entry in profile.auth.additional
                        ],
                    },
                    "headers": [
                        {
                            "name": header.name,
                            "value": header.value,
                            "value_env": header.value_env,
                        }
                        for header in profile.headers
                    ],
                }
            )
        payload = {
            "provider": {
                "id": provider.id,
                "base_url": provider.base_url,
                "protocols": sorted(str(protocol) for protocol in provider.protocols),
            },
            "profiles": profile_rows,
            "model_preference": (
                None
                if model_preference is None
                else {
                    "preferred_surface": model_preference.preferred_surface,
                    "fixed": model_preference.fixed,
                }
            ),
            "request_constraints": {
                "allowed_surfaces": (
                    None if allowed_surfaces is None else sorted(allowed_surfaces)
                ),
                "metadata_surface": metadata_surface,
                "bundled_hint": (
                    bundled_hint
                    if isinstance(bundled_hint, str) or bundled_hint is None
                    else {
                        "provider_id": bundled_hint.provider_id,
                        "model_id": bundled_hint.model_id,
                        "preferred_surface": bundled_hint.preferred_surface,
                        "verified_on": bundled_hint.verified_on,
                        "source": bundled_hint.source,
                    }
                ),
            },
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def resolve(
        self,
        provider: ProviderConfig,
        model_id: str,
        *,
        profiles: Iterable[WireProfile] | None = None,
        metadata_surface: WireSurfaceName | None = None,
        bundled_hint: WireHint | WireSurfaceName | None = None,
        allowed_surfaces: Iterable[WireSurfaceName] | None = None,
        learned_preference_ttl_s: float = 86400.0,
        now_monotonic: float | None = None,
        now_epoch: float | None = None,
    ) -> WireResolution:
        """Return a stable, suppression-aware candidate order."""
        all_profiles = tuple(
            profiles
            if profiles is not None
            else resolve_provider_wire_profiles(provider)
        )
        preference = provider.model_wire.get(model_id)
        allowed = set(allowed_surfaces) if allowed_surfaces is not None else None
        fingerprint = self.candidate_fingerprint(
            provider,
            all_profiles,
            model_preference=preference,
            allowed_surfaces=allowed,
            metadata_surface=metadata_surface,
            bundled_hint=bundled_hint,
        )
        selected_profiles = all_profiles
        if allowed is not None:
            selected_profiles = tuple(
                profile for profile in selected_profiles if profile.surface in allowed
            )
        key = (provider.id, model_id, fingerprint)
        monotonic_now = time.monotonic() if now_monotonic is None else now_monotonic
        with self._state_lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
            candidates: dict[WireSurfaceName, WireProfile] = {
                profile.surface: profile for profile in selected_profiles
            }
            if preference is not None and preference.preferred_surface in candidates:
                ordered, source = self._ordered_from_preference(
                    candidates,
                    preference,
                    entry,
                    metadata_surface=metadata_surface,
                    bundled_hint=bundled_hint,
                    now_monotonic=monotonic_now,
                    learned_preference_ttl_s=learned_preference_ttl_s,
                )
            else:
                ordered, source = self._ordered_from_hints(
                    candidates,
                    entry,
                    metadata_surface=metadata_surface,
                    bundled_hint=bundled_hint,
                    now_monotonic=monotonic_now,
                    learned_preference_ttl_s=learned_preference_ttl_s,
                )
            if preference is not None and preference.fixed and ordered:
                ordered = ordered[:1]
                source = "operator_fixed"
            self._increment_metric("wire_selection_source", source)
        return WireResolution(
            provider_id=provider.id,
            model_id=model_id,
            candidate_fingerprint=fingerprint,
            candidates=tuple(ordered),
            selected_source=source,
            fixed=bool(preference and preference.fixed),
        )

    def configure(
        self,
        *,
        cache_max_entries: int,
        max_concurrent_per_provider: int = 1,
        min_negotiation_interval_s: float = 1.0,
        rejection_cooldown_s: float = 300.0,
        learned_preference_ttl_s: float = 86400.0,
    ) -> None:
        """Apply live bounded-cache settings for the current generation."""
        with self._state_lock:
            self._cache_max_entries = max(1, cache_max_entries)
            self._max_concurrent_per_provider = max(1, max_concurrent_per_provider)
            self._min_negotiation_interval_s = max(0.0, min_negotiation_interval_s)
            self._rejection_cooldown_s = max(0.0, rejection_cooldown_s)
            self._learned_preference_ttl_s = max(0.0, learned_preference_ttl_s)
            self._evict_if_needed()

    def record_success(
        self,
        provider_id: str,
        model_id: str,
        candidate_fingerprint: str,
        surface: WireSurfaceName,
        *,
        source: str = "learned_runtime",
        now_monotonic: float | None = None,
        now_epoch: float | None = None,
    ) -> None:
        """Refresh a learned preference after a completed ordinary request."""
        now_mono = time.monotonic() if now_monotonic is None else now_monotonic
        now_wall = time.time() if now_epoch is None else now_epoch
        key = (provider_id, model_id, candidate_fingerprint)
        with self._state_lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = WirePreferenceEntry(
                    preferred_surface=surface,
                    last_success_monotonic=now_mono,
                    last_success_epoch=now_wall,
                    source=source,
                    confidence_timestamp=now_wall,
                )
                self._entries[key] = entry
            else:
                entry.preferred_surface = surface
                entry.last_success_monotonic = now_mono
                entry.last_success_epoch = now_wall
                entry.source = source
                entry.confidence_timestamp = now_wall
                entry.candidate_rejections.pop(surface, None)
                self._entries.move_to_end(key)
            self._evict_if_needed()
            self._increment_metric("wire_surface_selected", surface)

    def record_deterministic_rejection(
        self,
        provider_id: str,
        model_id: str,
        candidate_fingerprint: str,
        surface: WireSurfaceName,
        *,
        rejection_class: str,
        cooldown_s: float,
        now_monotonic: float | None = None,
    ) -> None:
        """Record only a caller-authorized structural candidate rejection."""
        now = time.monotonic() if now_monotonic is None else now_monotonic
        key = (provider_id, model_id, candidate_fingerprint)
        bounded_cooldown = max(0.0, min(cooldown_s, 1800.0))
        with self._state_lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = WirePreferenceEntry(
                    preferred_surface=surface,
                    last_success_monotonic=now,
                    last_success_epoch=time.time(),
                    source="candidate_rejection",
                    confidence_timestamp=time.time(),
                )
                self._entries[key] = entry
            entry.candidate_rejections[surface] = CandidateRejection(
                last_deterministic_rejection_at=now,
                suppress_until=now + bounded_cooldown,
                last_rejection_class=rejection_class[:80] or "unknown",
            )
            self._entries.move_to_end(key)
            self._evict_if_needed()
            self._increment_metric("wire_candidate_rejected", rejection_class[:80])

    def is_suppressed(
        self,
        provider_id: str,
        model_id: str,
        candidate_fingerprint: str,
        surface: WireSurfaceName,
        *,
        now_monotonic: float | None = None,
    ) -> bool:
        """Return whether a candidate is in its temporary negative cache."""
        now = time.monotonic() if now_monotonic is None else now_monotonic
        entry = self._entries.get((provider_id, model_id, candidate_fingerprint))
        rejection = entry.candidate_rejections.get(surface) if entry else None
        return rejection is not None and rejection.suppress_until > now

    async def begin_negotiation(
        self,
        resolution: WireResolution,
        *,
        max_concurrent_per_provider: int | None = None,
        min_negotiation_interval_s: float | None = None,
        now_monotonic: float | None = None,
    ) -> NegotiationHandle:
        """Join or create one provider/model negotiation flight."""
        loop = asyncio.get_running_loop()
        now = time.monotonic() if now_monotonic is None else now_monotonic
        flight_key = (resolution.provider_id, resolution.model_id)
        with self._state_lock:
            max_concurrent = (
                self._max_concurrent_per_provider
                if max_concurrent_per_provider is None
                else max_concurrent_per_provider
            )
            min_interval = (
                self._min_negotiation_interval_s
                if min_negotiation_interval_s is None
                else min_negotiation_interval_s
            )
            existing = self._flights.get(flight_key)
            if existing is not None:
                self._increment_metric("wire_singleflight_follower", "follower")
                return NegotiationHandle(
                    self,
                    resolution.provider_id,
                    resolution.model_id,
                    existing.candidate_fingerprint,
                    "follower",
                    existing.future,
                    max_concurrent,
                    min_interval,
                )
            if self._next_negotiation_allowed_at.get(resolution.provider_id, 0.0) > now:
                future = loop.create_future()
                future.set_result(
                    WireNegotiationResult(
                        resolution.provider_id,
                        resolution.model_id,
                        None,
                        "throttled",
                    )
                )
                self._increment_metric("wire_negotiation_result", "throttled")
                return NegotiationHandle(
                    self,
                    resolution.provider_id,
                    resolution.model_id,
                    resolution.candidate_fingerprint,
                    "throttled",
                    future,
                    max_concurrent,
                    min_interval,
                )
            future = loop.create_future()
            self._flights[flight_key] = _NegotiationFlight(
                future, resolution.candidate_fingerprint
            )
            self._increment_metric("wire_negotiation_attempted", "leader")
        return NegotiationHandle(
            self,
            resolution.provider_id,
            resolution.model_id,
            resolution.candidate_fingerprint,
            "leader",
            future,
            max_concurrent,
            min_interval,
        )

    def delay_provider_negotiation(self, provider_id: str, delay_s: float) -> None:
        """Advance the provider-wide negotiation-only pressure timestamp."""
        now = time.monotonic()
        with self._state_lock:
            self._next_negotiation_allowed_at[provider_id] = max(
                self._next_negotiation_allowed_at.get(provider_id, 0.0),
                now + max(0.0, min(delay_s, 1800.0)),
            )

    def snapshot(self) -> dict[str, object]:
        """Return bounded diagnostics without exposing request or credential data."""
        with self._state_lock:
            return {
                "cache_entries": len(self._entries),
                "inflight": len(self._flights),
                "provider_gates": len(self._provider_gates),
                "next_negotiation_allowed_providers": len(
                    self._next_negotiation_allowed_at
                ),
                "counters": dict(self._metrics),
            }

    def _ordered_from_preference(
        self,
        candidates: Mapping[WireSurfaceName, WireProfile],
        preference: ModelWirePreference,
        entry: WirePreferenceEntry | None,
        *,
        metadata_surface: WireSurfaceName | None,
        bundled_hint: WireHint | WireSurfaceName | None,
        now_monotonic: float,
        learned_preference_ttl_s: float,
    ) -> tuple[list[WireProfile], str]:
        if (
            entry is not None
            and entry.preferred_surface in candidates
            and not self._is_suppressed(entry, entry.preferred_surface, now_monotonic)
        ):
            static_hint = self._strongest_hint(
                candidates,
                metadata_surface=metadata_surface,
                bundled_hint=bundled_hint,
            )
            ttl_s = max(0.0, learned_preference_ttl_s)
            stale = now_monotonic - entry.last_success_monotonic > ttl_s
            if not stale or static_hint is None:
                return self._remaining(
                    candidates, entry.preferred_surface, entry, now_monotonic
                ), ("learned_runtime" if not stale else "learned_stale")
        chosen = preference.preferred_surface
        if not self._is_suppressed(entry, chosen, now_monotonic):
            return (
                self._remaining(candidates, chosen, entry, now_monotonic),
                "operator_preference",
            )
        return self._ordered_from_hints(
            candidates,
            entry,
            metadata_surface=metadata_surface,
            bundled_hint=bundled_hint,
            now_monotonic=now_monotonic,
            learned_preference_ttl_s=learned_preference_ttl_s,
        )

    def _ordered_from_hints(
        self,
        candidates: Mapping[WireSurfaceName, WireProfile],
        entry: WirePreferenceEntry | None,
        *,
        metadata_surface: WireSurfaceName | None,
        bundled_hint: WireHint | WireSurfaceName | None,
        now_monotonic: float,
        learned_preference_ttl_s: float,
    ) -> tuple[list[WireProfile], str]:
        if (
            entry is not None
            and entry.preferred_surface in candidates
            and not self._is_suppressed(entry, entry.preferred_surface, now_monotonic)
        ):
            static_hint = self._strongest_hint(
                candidates,
                metadata_surface=metadata_surface,
                bundled_hint=bundled_hint,
            )
            stale = now_monotonic - entry.last_success_monotonic > max(
                0.0, learned_preference_ttl_s
            )
            if not stale or static_hint is None:
                return self._remaining(
                    candidates, entry.preferred_surface, entry, now_monotonic
                ), ("learned_runtime" if not stale else "learned_stale")
        hint = self._strongest_hint(
            candidates,
            metadata_surface=metadata_surface,
            bundled_hint=bundled_hint,
        )
        if hint is not None and not self._is_suppressed(entry, hint, now_monotonic):
            return self._remaining(candidates, hint, entry, now_monotonic), (
                "catalog_hint" if metadata_surface == hint else "bundled_hint"
            )
        available = [
            profile
            for profile in candidates.values()
            if not self._is_suppressed(entry, profile.surface, now_monotonic)
        ]
        return available, "provider_priority"

    @staticmethod
    def _strongest_hint(
        candidates: Mapping[WireSurfaceName, WireProfile],
        *,
        metadata_surface: WireSurfaceName | None,
        bundled_hint: WireHint | WireSurfaceName | None,
    ) -> WireSurfaceName | None:
        if metadata_surface in candidates:
            return metadata_surface
        if isinstance(bundled_hint, str):
            bundled_surface = bundled_hint
        elif bundled_hint is None:
            bundled_surface = None
        else:
            bundled_surface = bundled_hint.preferred_surface
        if bundled_surface in candidates:
            return cast("WireSurfaceName", bundled_surface)
        return None

    @classmethod
    def _remaining(
        cls,
        candidates: Mapping[WireSurfaceName, WireProfile],
        first: WireSurfaceName,
        entry: WirePreferenceEntry | None,
        now_monotonic: float,
    ) -> list[WireProfile]:
        remaining = [
            profile
            for profile in candidates.values()
            if profile.surface != first
            and not cls._is_suppressed(entry, profile.surface, now_monotonic)
        ]
        selected = candidates.get(first)
        return ([] if selected is None else [selected]) + remaining

    @staticmethod
    def _is_suppressed(
        entry: WirePreferenceEntry | None,
        surface: WireSurfaceName,
        now_monotonic: float,
    ) -> bool:
        if entry is None:
            return False
        rejection = entry.candidate_rejections.get(surface)
        return rejection is not None and rejection.suppress_until > now_monotonic

    def provider_gate(self, provider_id: str) -> _ProviderGate:
        with self._state_lock:
            gate = self._provider_gates.get(provider_id)
            if gate is None:
                gate = _ProviderGate()
                self._provider_gates[provider_id] = gate
            return gate

    def mark_negotiation_started(self, provider_id: str, interval_s: float) -> None:
        with self._state_lock:
            self._next_negotiation_allowed_at[provider_id] = time.monotonic() + max(
                0.0, min(interval_s, 1800.0)
            )

    def finish_flight(
        self, handle: NegotiationHandle, result: WireNegotiationResult
    ) -> None:
        key = (handle.provider_id, handle.model_id)
        with self._state_lock:
            flight = self._flights.get(key)
            if flight is not None and flight.future is handle.future:
                self._flights.pop(key, None)
                if not flight.future.done():
                    flight.future.set_result(result)
                self._increment_metric("wire_negotiation_result", result.result)

    def _increment_metric(self, name: str, value: str) -> None:
        key = f"{name}:{value[:80]}"
        if key not in self._metrics and len(self._metrics) >= 256:
            self._metrics.pop(next(iter(self._metrics)))
        self._metrics[key] = self._metrics.get(key, 0) + 1

    def _evict_if_needed(self) -> None:
        while len(self._entries) > self._cache_max_entries:
            self._entries.popitem(last=False)


__all__ = [
    "CandidateRejection",
    "NegotiationHandle",
    "WireNegotiationResult",
    "WirePreferenceEntry",
    "WireProfileResolver",
    "WireResolution",
]
