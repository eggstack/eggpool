"""Bounded model quarantine state machine.

Replaces indefinite ``disable_model()`` on runtime-only model-like
failures with a state machine that requires corroboration before
becoming terminal and automatically clears when authoritative evidence
or successful traffic demonstrates recovery.

States::

    healthy → suspected → quarantined → terminal_withdrawn
                      ↘              ↘
                       expired → healthy
                                  (auto-recovery)

Properties:

* Keyed by (provider_id, account_id, canonical_model_id,
  upstream_model_id, upstream_protocol).
* First observation creates ``suspected`` with short TTL.
* Repeated equivalent evidence within window promotes to
  ``quarantined`` with longer TTL.
* Expiry restores eligibility automatically.
* Exact-key success clears bounded quarantine.
* Provider catalog reappearance clears bounded quarantine.
* Terminal withdrawal requires authoritative or explicit operator
  evidence by default.
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from dataclasses import dataclass, field
from enum import StrEnum

logger = logging.getLogger(__name__)


class QuarantineState(StrEnum):
    """Model quarantine lifecycle states."""

    HEALTHY = "healthy"
    SUSPECTED = "suspected"
    QUARANTINED = "quarantined"
    TERMINAL_WITHDRAWN = "terminal_withdrawn"


class EvidenceProvenance(StrEnum):
    """Source of the evidence that produced the quarantine entry."""

    RUNTIME_HTTP = "runtime_http"
    PROVIDER_CATALOG = "provider_catalog"
    MODEL_INFO = "model_info"
    MANUAL_OVERRIDE = "manual_override"
    OPERATOR_ACTION = "operator_action"
    MIGRATION_LEGACY = "migration_legacy"


@dataclass
class QuarantineEntry:
    """A single model quarantine record."""

    state: QuarantineState
    provider_id: str
    account_id: str
    canonical_model_id: str
    upstream_model_id: str | None
    upstream_protocol: str
    evidence_provenance: EvidenceProvenance
    reason: str
    first_observed: float
    last_observed: float
    observation_count: int
    expiry: float | None
    cleared_at: float | None = None
    clear_reason: str | None = None
    last_status_code: int | None = None
    last_error_class: str | None = None


def _quarantine_key(
    provider_id: str,
    account_id: str,
    canonical_model_id: str,
    upstream_model_id: str | None,
    upstream_protocol: str,
) -> str:
    """Deterministic key for a quarantine entry."""
    parts = (
        provider_id,
        account_id,
        canonical_model_id,
        upstream_model_id or "",
        upstream_protocol,
    )
    return hashlib.sha256(":".join(parts).encode()).hexdigest()[:32]


# Default TTLs (seconds)
DEFAULT_SUSPECTED_TTL = 120.0  # 2 minutes
DEFAULT_QUARANTINED_TTL = 300.0  # 5 minutes
DEFAULT_PROMOTION_THRESHOLD = 2  # observations to promote suspected → quarantined

# Lifecycle advancement rank used to keep hydration from demoting
# newer runtime state.
_STATE_RANK: dict[QuarantineState, int] = {
    QuarantineState.SUSPECTED: 0,
    QuarantineState.QUARANTINED: 1,
    QuarantineState.TERMINAL_WITHDRAWN: 2,
}


@dataclass
class ModelQuarantine:
    """In-memory bounded model quarantine state machine.

    Maintains a dict of quarantine entries keyed by a deterministic
    hash of (provider_id, account_id, canonical_model_id,
    upstream_model_id, upstream_protocol).  Expired entries are pruned
    lazily on access or by explicit :meth:`prune_expired`.
    """

    _entries: dict[str, QuarantineEntry] = field(default_factory=dict)  # pyright: ignore[reportUnknownVariableType]
    suspected_ttl: float = DEFAULT_SUSPECTED_TTL
    quarantined_ttl: float = DEFAULT_QUARANTINED_TTL
    promotion_threshold: int = DEFAULT_PROMOTION_THRESHOLD

    def is_model_quarantined(
        self,
        provider_id: str,
        account_id: str,
        canonical_model_id: str,
        upstream_model_id: str | None,
        upstream_protocol: str,
        *,
        now: float | None = None,
    ) -> bool:
        """Check if a model is quarantined for the given scope key.

        Returns ``False`` for healthy, expired, or terminal-withdrawn
        entries.  Terminal withdrawal is a separate routing concern
        (handled by the catalog); quarantine only suppresses bounded
        entries.
        """
        if now is None:
            now = time.time()
        key = _quarantine_key(
            provider_id,
            account_id,
            canonical_model_id,
            upstream_model_id,
            upstream_protocol,
        )
        entry = self._entries.get(key)
        if entry is None:
            return False
        if entry.state == QuarantineState.HEALTHY:
            return False
        if entry.state == QuarantineState.TERMINAL_WITHDRAWN:
            # Terminal withdrawal is handled by catalog/eligibility,
            # not quarantine — quarantine only suppresses bounded.
            return False
        if entry.expiry is not None and now >= entry.expiry:
            self._expire_entry(key, entry, now)
            return False
        return True

    def record_observation(
        self,
        *,
        provider_id: str,
        account_id: str,
        canonical_model_id: str,
        upstream_model_id: str | None,
        upstream_protocol: str,
        evidence_provenance: EvidenceProvenance,
        reason: str,
        status_code: int | None = None,
        error_class: str | None = None,
        now: float | None = None,
    ) -> QuarantineEntry:
        """Record a runtime observation and promote state if warranted.

        Returns the (possibly updated) quarantine entry.
        """
        if now is None:
            now = time.time()
        key = _quarantine_key(
            provider_id,
            account_id,
            canonical_model_id,
            upstream_model_id,
            upstream_protocol,
        )
        existing = self._entries.get(key)

        if existing is None or existing.state == QuarantineState.HEALTHY:
            # Fresh suspected entry
            entry = QuarantineEntry(
                state=QuarantineState.SUSPECTED,
                provider_id=provider_id,
                account_id=account_id,
                canonical_model_id=canonical_model_id,
                upstream_model_id=upstream_model_id,
                upstream_protocol=upstream_protocol,
                evidence_provenance=evidence_provenance,
                reason=reason,
                first_observed=now,
                last_observed=now,
                observation_count=1,
                expiry=now + self.suspected_ttl,
                last_status_code=status_code,
                last_error_class=error_class,
            )
            self._entries[key] = entry
            logger.info(
                "model_quarantine: suspected model=%s provider=%s account=%s reason=%s",
                canonical_model_id,
                provider_id,
                account_id,
                reason,
            )
            return entry

        # Existing entry — check expiry first
        if existing.expiry is not None and now >= existing.expiry:
            self._expire_entry(key, existing, now)
            # Re-create as fresh suspected
            entry = QuarantineEntry(
                state=QuarantineState.SUSPECTED,
                provider_id=provider_id,
                account_id=account_id,
                canonical_model_id=canonical_model_id,
                upstream_model_id=upstream_model_id,
                upstream_protocol=upstream_protocol,
                evidence_provenance=evidence_provenance,
                reason=reason,
                first_observed=now,
                last_observed=now,
                observation_count=1,
                expiry=now + self.suspected_ttl,
                last_status_code=status_code,
                last_error_class=error_class,
            )
            self._entries[key] = entry
            return entry

        # Accumulate observation
        existing.last_observed = now
        existing.observation_count += 1
        existing.last_status_code = status_code
        existing.last_error_class = error_class
        existing.reason = reason

        # Promote suspected → quarantined if threshold reached
        if (
            existing.state == QuarantineState.SUSPECTED
            and existing.observation_count >= self.promotion_threshold
        ):
            existing.state = QuarantineState.QUARANTINED
            existing.expiry = now + self.quarantined_ttl
            logger.warning(
                "model_quarantine: quarantined model=%s provider=%s account=%s "
                "observations=%d reason=%s",
                canonical_model_id,
                provider_id,
                account_id,
                existing.observation_count,
                reason,
            )

        return existing

    def clear_exact_key(
        self,
        provider_id: str,
        account_id: str,
        canonical_model_id: str,
        upstream_model_id: str | None,
        upstream_protocol: str,
        reason: str = "successful_request",
        *,
        now: float | None = None,
    ) -> bool:
        """Clear bounded quarantine for the exact scope key on success.

        Returns ``True`` if an entry was cleared, ``False`` if none
        existed or was already healthy/expired.
        """
        if now is None:
            now = time.time()
        key = _quarantine_key(
            provider_id,
            account_id,
            canonical_model_id,
            upstream_model_id,
            upstream_protocol,
        )
        entry = self._entries.get(key)
        if entry is None or entry.state in (
            QuarantineState.HEALTHY,
            QuarantineState.TERMINAL_WITHDRAWN,
        ):
            return False
        if entry.expiry is not None and now >= entry.expiry:
            self._expire_entry(key, entry, now)
            return False
        entry.state = QuarantineState.HEALTHY
        entry.cleared_at = now
        entry.clear_reason = reason
        logger.info(
            "model_quarantine: cleared model=%s provider=%s account=%s reason=%s",
            canonical_model_id,
            provider_id,
            account_id,
            reason,
        )
        return True

    def set_terminal_withdrawn(
        self,
        provider_id: str,
        account_id: str,
        canonical_model_id: str,
        upstream_model_id: str | None,
        upstream_protocol: str,
        reason: str = "authoritative_catalog_absence",
        provenance: EvidenceProvenance = EvidenceProvenance.PROVIDER_CATALOG,
        *,
        now: float | None = None,
    ) -> QuarantineEntry:
        """Mark a model as terminal withdrawn (authoritative).

        Only authoritative sources (provider catalog, operator action,
        manual override) may produce terminal withdrawal.
        """
        if now is None:
            now = time.time()
        key = _quarantine_key(
            provider_id,
            account_id,
            canonical_model_id,
            upstream_model_id,
            upstream_protocol,
        )
        entry = QuarantineEntry(
            state=QuarantineState.TERMINAL_WITHDRAWN,
            provider_id=provider_id,
            account_id=account_id,
            canonical_model_id=canonical_model_id,
            upstream_model_id=upstream_model_id,
            upstream_protocol=upstream_protocol,
            evidence_provenance=provenance,
            reason=reason,
            first_observed=now,
            last_observed=now,
            observation_count=1,
            expiry=None,  # terminal — no auto-expiry
        )
        self._entries[key] = entry
        logger.warning(
            "model_quarantine: terminal_withdrawn model=%s provider=%s account=%s "
            "provenance=%s reason=%s",
            canonical_model_id,
            provider_id,
            account_id,
            provenance.value,
            reason,
        )
        return entry

    def clear_authoritative_reappearance(
        self,
        provider_id: str,
        account_id: str,
        canonical_model_id: str,
        upstream_model_id: str | None,
        upstream_protocol: str,
        *,
        now: float | None = None,
    ) -> bool:
        """Clear quarantine when the model reappears in the provider catalog.

        Returns ``True`` if an entry was cleared.
        """
        if now is None:
            now = time.time()
        key = _quarantine_key(
            provider_id,
            account_id,
            canonical_model_id,
            upstream_model_id,
            upstream_protocol,
        )
        entry = self._entries.get(key)
        if entry is None or entry.state == QuarantineState.HEALTHY:
            return False
        entry.state = QuarantineState.HEALTHY
        entry.cleared_at = now
        entry.clear_reason = "catalog_reappearance"
        logger.info(
            "model_quarantine: catalog_reappearance model=%s provider=%s account=%s",
            canonical_model_id,
            provider_id,
            account_id,
        )
        return True

    def manual_clear(
        self,
        provider_id: str,
        account_id: str,
        canonical_model_id: str,
        upstream_model_id: str | None,
        upstream_protocol: str,
        *,
        now: float | None = None,
    ) -> bool:
        """Operator-initiated clear of any quarantine state.

        Returns ``True`` if an entry was cleared.
        """
        if now is None:
            now = time.time()
        key = _quarantine_key(
            provider_id,
            account_id,
            canonical_model_id,
            upstream_model_id,
            upstream_protocol,
        )
        entry = self._entries.get(key)
        if entry is None or entry.state == QuarantineState.HEALTHY:
            return False
        entry.state = QuarantineState.HEALTHY
        entry.cleared_at = now
        entry.clear_reason = "operator_clear"
        logger.info(
            "model_quarantine: operator_clear model=%s provider=%s account=%s",
            canonical_model_id,
            provider_id,
            account_id,
        )
        return True

    def list_entries(
        self,
        *,
        include_expired: bool = False,
        now: float | None = None,
    ) -> list[QuarantineEntry]:
        """Return all quarantine entries.

        Expired entries are excluded unless ``include_expired`` is set.
        """
        if now is None:
            now = time.time()
        result: list[QuarantineEntry] = []
        for entry in self._entries.values():
            if entry.state == QuarantineState.HEALTHY:
                continue
            if not include_expired and entry.expiry is not None and now >= entry.expiry:
                continue
            result.append(entry)
        return result

    def prune_expired(self, *, now: float | None = None) -> int:
        """Remove expired entries. Returns count removed."""
        if now is None:
            now = time.time()
        expired_keys = [
            key
            for key, entry in self._entries.items()
            if entry.state != QuarantineState.HEALTHY
            and entry.expiry is not None
            and now >= entry.expiry
        ]
        for key in expired_keys:
            del self._entries[key]
        return len(expired_keys)

    def get_entry(
        self,
        provider_id: str,
        account_id: str,
        canonical_model_id: str,
        upstream_model_id: str | None,
        upstream_protocol: str,
    ) -> QuarantineEntry | None:
        """Get the quarantine entry for a specific scope key, or None."""
        key = _quarantine_key(
            provider_id,
            account_id,
            canonical_model_id,
            upstream_model_id,
            upstream_protocol,
        )
        return self._entries.get(key)

    def hydrate_entry(
        self,
        entry: QuarantineEntry,
        *,
        now: float | None = None,
    ) -> None:
        """Hydrate a quarantine entry from durable storage.

        Used during startup to restore persisted quarantine state.
        Expired entries are skipped.  When an entry already exists for
        the same scope key, a stale durable row never demotes newer
        runtime state: a runtime-cleared (healthy) or terminal entry is
        kept as-is, and hydration otherwise only replaces entries whose
        stored (state rank, observation count) is strictly more advanced
        than what is already resident.
        """
        if now is None:
            now = time.time()
        if entry.state == QuarantineState.HEALTHY:
            return
        if entry.expiry is not None and now >= entry.expiry:
            return
        key = _quarantine_key(
            entry.provider_id,
            entry.account_id,
            entry.canonical_model_id,
            entry.upstream_model_id,
            entry.upstream_protocol,
        )
        existing = self._entries.get(key)
        if existing is not None:
            # A runtime-cleared (healthy) entry is never resurrected by
            # a stale durable row; terminal withdrawal likewise wins.
            if existing.state in (
                QuarantineState.HEALTHY,
                QuarantineState.TERMINAL_WITHDRAWN,
            ):
                return
            existing_rank = _STATE_RANK[existing.state]
            incoming_rank = _STATE_RANK[entry.state]
            if existing_rank > incoming_rank or (
                existing_rank == incoming_rank
                and existing.observation_count >= entry.observation_count
            ):
                return
        self._entries[key] = entry

    def _expire_entry(self, key: str, entry: QuarantineEntry, now: float) -> None:
        """Mark an entry as expired and remove it from the dict."""
        logger.debug(
            "model_quarantine: expired model=%s provider=%s account=%s state=%s",
            entry.canonical_model_id,
            entry.provider_id,
            entry.account_id,
            entry.state.value,
        )
        del self._entries[key]


def entry_from_row(row: dict[str, object]) -> QuarantineEntry:
    """Construct a :class:`QuarantineEntry` from a repository row.

    The :class:`eggpool.db.repositories.ModelQuarantineRepository` returns
    plain dicts with epoch floats (no SQLite timestamp parsing).  This
    helper centralises the conversion so callers do not need to know
    about :class:`EvidenceProvenance` or :class:`QuarantineState` enum
    parsing. Invalid rows are rejected because hydration is a
    generation-publication prerequisite; silently converting an unknown
    state or malformed identity into a healthy entry would be fail-open.
    """

    def _required_text(field_name: str) -> str:
        value = row.get(field_name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"invalid quarantine {field_name}")
        return value

    state_str = _required_text("state")
    try:
        state = QuarantineState(state_str)
    except ValueError:
        raise ValueError("invalid quarantine state") from None
    provenance_str = _required_text("evidence_provenance")
    try:
        provenance = EvidenceProvenance(provenance_str)
    except ValueError:
        raise ValueError("invalid quarantine evidence provenance") from None

    def _epoch(field_name: str, *, required: bool = False) -> float | None:
        value = row.get(field_name)
        if value is None:
            if required:
                raise ValueError(f"missing quarantine {field_name}")
            return None
        try:
            result = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ValueError(f"invalid quarantine {field_name}") from None
        if not math.isfinite(result):
            raise ValueError(f"invalid quarantine {field_name}")
        return result

    def _int(field_name: str, *, required: bool = False) -> int | None:
        value = row.get(field_name)
        if value is None:
            if required:
                raise ValueError(f"missing quarantine {field_name}")
            return None
        try:
            result = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            raise ValueError(f"invalid quarantine {field_name}") from None
        if isinstance(value, bool) or result < 1:
            raise ValueError(f"invalid quarantine {field_name}")
        return result

    upstream_model_id = row.get("upstream_model_id")
    if upstream_model_id is not None and not isinstance(upstream_model_id, str):
        raise ValueError("invalid quarantine upstream_model_id")

    upstream_protocol = _required_text("upstream_protocol")
    provider_id = _required_text("provider_id")
    account_id = _required_text("account_id")
    canonical_model_id = _required_text("canonical_model_id")
    reason = row.get("reason")
    if not isinstance(reason, str):
        raise ValueError("invalid quarantine reason")
    clear_reason = row.get("clear_reason")
    if clear_reason is not None and not isinstance(clear_reason, str):
        raise ValueError("invalid quarantine clear reason")
    last_error_class = row.get("last_error_class")
    if last_error_class is not None and not isinstance(last_error_class, str):
        raise ValueError("invalid quarantine error class")

    return QuarantineEntry(
        state=state,
        provider_id=provider_id,
        account_id=account_id,
        canonical_model_id=canonical_model_id,
        upstream_model_id=upstream_model_id,
        upstream_protocol=upstream_protocol,
        evidence_provenance=provenance,
        reason=reason,
        first_observed=_epoch("first_observed_epoch", required=True) or 0.0,
        last_observed=_epoch("last_observed_epoch", required=True) or 0.0,
        observation_count=_int("observation_count", required=True) or 1,
        expiry=_epoch("expiry_epoch"),
        cleared_at=_epoch("cleared_at_epoch"),
        clear_reason=clear_reason,
        last_status_code=_int("last_status_code"),
        last_error_class=last_error_class,
    )
