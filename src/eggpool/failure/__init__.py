"""Typed failure effects and bounded model quarantine.

Centralizes the consequences of request and upstream failures into one
typed, test-pinned decision.  Replaces first-observation indefinite
model withdrawal with bounded, provider/account/model/protocol-scoped
quarantine that requires corroboration before becoming terminal and
automatically clears when authoritative evidence or successful traffic
demonstrates recovery.
"""

from __future__ import annotations

from eggpool.failure.applier import (
    AppliedEffects,
    EffectsApplier,
    FailureEffectProgress,
)
from eggpool.failure.effects import FailureDecision, FailureEffects
from eggpool.failure.observation import FailureObservation
from eggpool.failure.quarantine import (
    EvidenceProvenance,
    ModelQuarantine,
    QuarantineEntry,
    QuarantineState,
    entry_from_row,
)
from eggpool.failure.signal import FailureSignal

__all__ = [
    "AppliedEffects",
    "EffectsApplier",
    "FailureEffectProgress",
    "EvidenceProvenance",
    "FailureEffects",
    "FailureDecision",
    "FailureObservation",
    "FailureSignal",
    "ModelQuarantine",
    "QuarantineEntry",
    "QuarantineState",
    "entry_from_row",
]
