"""Process-local semantic affinity for sticky model routers.

Affinity is deliberately separate from provider/account routing.  It remembers
only the concrete model selected for a bounded, hashed session identity and
lets the ordinary coordinator continue to make the account/provider choice.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from eggpool.model_router.registry import CompiledModelRouter
    from eggpool.model_router.selector import ModelSelection
    from eggpool.wire.ir import CanonicalRequest

AFFINITY_CACHE_MAX_ENTRIES: Final = 4096
AFFINITY_SESSION_HEADER: Final = "x-eggpool-route-session"
AFFINITY_SESSION_HEADER_MAX_BYTES: Final = 512
AUTOMATIC_PREFIX_MAX_BYTES: Final = 4096

SessionSource = Literal["explicit_session", "automatic_session"]
AffinityDecisionSource = Literal["selector", "default"]


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    """A non-sensitive session identity used by the affinity cache."""

    digest: bytes
    source: SessionSource


@dataclass(frozen=True, slots=True)
class AffinityDecision:
    """The bounded derived state retained for one sticky route."""

    virtual_model: str
    router_fingerprint: str
    session_digest: bytes
    route_id: str
    route_label: str
    concrete_model: str
    source: AffinityDecisionSource
    expires_at_monotonic: float


@dataclass(frozen=True, slots=True)
class AffinityResolution:
    """One affinity result plus non-sensitive coordination facts."""

    decision: AffinityDecision
    cache_hit: bool
    single_flight_join: bool = False


@dataclass(frozen=True, slots=True)
class AffinityStats:
    """Aggregate cache and single-flight counters."""

    hits: int
    misses: int
    evictions: int
    expirations: int
    single_flight_leaders: int
    single_flight_joins: int


class _FlightAbortedError(Exception):
    """Internal signal allowing followers to retry after leader cancellation."""


def _consume_future_exception(future: asyncio.Future[AffinityDecision]) -> None:
    """Retrieve an exception from an abandoned flight future."""
    if not future.cancelled():
        future.exception()


def session_identity_from_header(value: str | None) -> SessionIdentity | None:
    """Hash one valid explicit session header, or ignore malformed input.

    Invalid values are intentionally treated as unavailable affinity.  This
    keeps an otherwise valid inference request deterministic while avoiding
    control-character and oversized identity inputs.
    """
    if value is None:
        return None
    if not value or len(value.encode("utf-8")) > AFFINITY_SESSION_HEADER_MAX_BYTES:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    return SessionIdentity(
        digest=hashlib.sha256(value.encode("utf-8")).digest(),
        source="explicit_session",
    )


def _normalize_identity_text(value: str) -> str:
    """Normalize harmless transport whitespace before semantic hashing."""
    return " ".join(value.replace("\r\n", "\n").replace("\r", "\n").split())


def _bounded_utf8(value: str, max_bytes: int) -> bytes:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return encoded
    return encoded[:max_bytes].decode("utf-8", errors="ignore").encode("utf-8")


def automatic_session_identity(
    request: CanonicalRequest,
    *,
    client_surface: str,
) -> SessionIdentity | None:
    """Derive a stable identity from the initial text-only conversation prefix.

    Responses is stateless by contract, so it requires the explicit header
    for cross-request stickiness.  Other surfaces use all system/developer
    text and the first user turn only.  Tool declarations/results, media,
    later turns, and generation options never enter the digest.
    """
    if client_surface == "responses":
        return None

    fields: list[tuple[str, str]] = []
    for message in request.messages:
        if message.role not in {"system", "developer"}:
            continue
        text = _normalize_identity_text(message.text())
        if text:
            fields.append((message.role, text))

    first_user_text = False
    for message in request.messages:
        if message.role != "user":
            continue
        text = _normalize_identity_text(message.text())
        if text:
            fields.append(("user", text))
            first_user_text = True
        break

    if not fields or not first_user_text:
        return None

    digest = hashlib.sha256()
    digest.update(b"eggpool-route-affinity/v1")
    digest.update(len(client_surface).to_bytes(2, "big"))
    digest.update(client_surface.encode("ascii"))
    remaining = AUTOMATIC_PREFIX_MAX_BYTES
    for role, text in fields:
        role_bytes = role.encode("ascii")
        text_bytes = _bounded_utf8(text, remaining)
        field_size = 2 + len(role_bytes) + 4 + len(text_bytes)
        if field_size > remaining:
            break
        digest.update(len(role_bytes).to_bytes(2, "big"))
        digest.update(role_bytes)
        digest.update(len(text_bytes).to_bytes(4, "big"))
        digest.update(text_bytes)
        remaining -= field_size

    return SessionIdentity(digest=digest.digest(), source="automatic_session")


class ModelRouterAffinity:
    """Bounded event-loop-local TTL/LRU cache with keyed single-flight."""

    def __init__(
        self,
        *,
        max_entries: int = AFFINITY_CACHE_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be positive")
        self.max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[tuple[str, str, bytes], AffinityDecision] = (
            OrderedDict()
        )
        self._flights: dict[
            tuple[str, str, bytes], asyncio.Future[AffinityDecision]
        ] = {}
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._expirations = 0
        self._single_flight_leaders = 0
        self._single_flight_joins = 0

    @property
    def entry_count(self) -> int:
        """Return the current number of live-or-lazily-expired entries."""
        return len(self._entries)

    @property
    def single_flight_count(self) -> int:
        """Return the number of currently coordinating keys."""
        return len(self._flights)

    @property
    def stats(self) -> AffinityStats:
        """Return aggregate counters without exposing per-session history."""
        return AffinityStats(
            hits=self._hits,
            misses=self._misses,
            evictions=self._evictions,
            expirations=self._expirations,
            single_flight_leaders=self._single_flight_leaders,
            single_flight_joins=self._single_flight_joins,
        )

    def _key(
        self,
        router: CompiledModelRouter,
        identity: SessionIdentity,
    ) -> tuple[str, str, bytes]:
        return (router.virtual_model, router.config_fingerprint, identity.digest)

    def _lookup(
        self,
        key: tuple[str, str, bytes],
    ) -> AffinityDecision | None:
        decision = self._entries.get(key)
        if decision is None:
            self._misses += 1
            return None
        if decision.expires_at_monotonic <= self._clock():
            del self._entries[key]
            self._expirations += 1
            self._misses += 1
            return None
        self._entries.move_to_end(key)
        self._hits += 1
        return decision

    def _store(
        self,
        key: tuple[str, str, bytes],
        decision: AffinityDecision,
    ) -> None:
        self._cleanup_expired(limit=16)
        if key in self._entries:
            del self._entries[key]
        while len(self._entries) >= self.max_entries:
            self._entries.popitem(last=False)
            self._evictions += 1
        self._entries[key] = decision

    def _cleanup_expired(self, *, limit: int) -> None:
        now = self._clock()
        for checked, (key, decision) in enumerate(
            tuple(self._entries.items()), start=1
        ):
            if checked > limit:
                break
            if decision.expires_at_monotonic <= now:
                del self._entries[key]
                self._expirations += 1

    @staticmethod
    def _decision_from_selection(
        router: CompiledModelRouter,
        identity: SessionIdentity,
        selection: ModelSelection,
        expires_at: float,
    ) -> AffinityDecision:
        route = router.route_by_id.get(selection.route_id)
        if (
            route is None
            or route.label != selection.route_label
            or route.model != selection.concrete_model
            or selection.virtual_model != router.virtual_model
        ):
            raise ValueError("model-router selection is not in the compiled route map")
        return AffinityDecision(
            virtual_model=router.virtual_model,
            router_fingerprint=router.config_fingerprint,
            session_digest=identity.digest,
            route_id=route.route_id,
            route_label=route.label,
            concrete_model=route.model,
            source=selection.source,
            expires_at_monotonic=expires_at,
        )

    async def resolve(
        self,
        router: CompiledModelRouter,
        identity: SessionIdentity,
        selector: Callable[[], Awaitable[ModelSelection]],
    ) -> AffinityResolution:
        """Resolve a sticky decision, single-flighting concurrent misses."""
        key = self._key(router, identity)
        while True:
            cached = self._lookup(key)
            if cached is not None:
                return AffinityResolution(decision=cached, cache_hit=True)

            future = self._flights.get(key)
            if future is not None:
                self._single_flight_joins += 1
                try:
                    decision = await asyncio.shield(future)
                except _FlightAbortedError:
                    # The cancelled leader does not own the decision.  A
                    # follower may become the next leader without deadlock.
                    continue
                return AffinityResolution(
                    decision=decision,
                    cache_hit=False,
                    single_flight_join=True,
                )

            # If the bounded coordination table is full, classify this key
            # directly.  The result remains eligible for the bounded cache.
            if len(self._flights) >= self.max_entries:
                selection = await selector()
                decision = self._decision_from_selection(
                    router,
                    identity,
                    selection,
                    self._clock() + router.affinity_ttl_s,
                )
                self._store(key, decision)
                return AffinityResolution(decision=decision, cache_hit=False)

            loop = asyncio.get_running_loop()
            future = loop.create_future()
            self._flights[key] = future
            self._single_flight_leaders += 1
            try:
                selection = await selector()
                decision = self._decision_from_selection(
                    router,
                    identity,
                    selection,
                    self._clock() + router.affinity_ttl_s,
                )
                self._store(key, decision)
                future.set_result(decision)
                return AffinityResolution(decision=decision, cache_hit=False)
            except asyncio.CancelledError:
                if not future.done():
                    future.set_exception(_FlightAbortedError())
                    future.add_done_callback(_consume_future_exception)
                raise
            except BaseException as exc:
                if not future.done():
                    future.set_exception(exc)
                    future.add_done_callback(_consume_future_exception)
                raise
            finally:
                if self._flights.get(key) is future:
                    del self._flights[key]


__all__ = [
    "AFFINITY_CACHE_MAX_ENTRIES",
    "AFFINITY_SESSION_HEADER",
    "AFFINITY_SESSION_HEADER_MAX_BYTES",
    "AUTOMATIC_PREFIX_MAX_BYTES",
    "AffinityDecision",
    "AffinityResolution",
    "AffinityStats",
    "ModelRouterAffinity",
    "SessionIdentity",
    "automatic_session_identity",
    "session_identity_from_header",
]
