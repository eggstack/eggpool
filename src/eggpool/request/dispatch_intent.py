"""Immutable persistence contracts for the durable dispatch write pipeline.

Milestone C (dispatch stability / durable write pipeline) replaces
per-request correctness-critical dispatch transactions with a
process-owned, bounded in-process persistence pipeline.  This module
owns the intent/result/error contracts that flow through the pipeline.

The :class:`DispatchIntent` is an immutable snapshot of every field
required to persist a request, reservation, and attempt bundle.  It
contains no API keys, no request bodies, no mutable references, and no
callbacks into the generation.

The :class:`PersistedDispatchResult` carries the durable IDs and
batch metadata back to the coordinator after a successful commit.
"""

from __future__ import annotations

import asyncio
import enum
import time
from dataclasses import dataclass, field


class DispatchWriterError(Exception):
    """Base class for dispatch writer pipeline errors."""


class DispatchQueueClosedError(DispatchWriterError):
    """Raised when attempting to enqueue to a closed writer."""


class DispatchQueueSaturatedError(DispatchWriterError):
    """Raised when the writer queue is full and the enqueue timeout elapses."""


class DispatchIntentCancelledError(DispatchWriterError):
    """Raised when an intent is cancelled before the writer accepts it."""


class DispatchTransactionError(DispatchWriterError):
    """Raised when a batch transaction fails."""


class DispatchAmbiguousCommitError(DispatchWriterError):
    """Raised when commit outcome cannot be determined."""


class DispatchValidationError(DispatchWriterError):
    """Raised when an intent fails invariant checks."""


class DispatchWriterShutdownError(DispatchWriterError):
    """Raised when the writer is shutting down and cannot accept work."""


class DispatchWriterLoopError(DispatchWriterError):
    """Raised when a writer is used from an event loop other than its owner."""


class DispatchIntentState(enum.Enum):
    """Lifecycle states for a dispatched intent within the writer."""

    QUEUED = "queued"
    CLAIMED = "claimed"
    PERSISTING = "persisting"
    COMMITTED = "committed"
    RESULT_DELIVERED = "result_delivered"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class DispatchIntent:
    """Immutable snapshot of all fields needed to persist a dispatch bundle.

    Constructed by the coordinator after routing and selection are
    complete.  Contains no API keys, no request bodies, no mutable
    references to generation-owned state.
    """

    proxy_request_id: str
    attempt_number: int
    account_id: int
    account_name: str
    provider_id: str
    model_id: str
    protocol: str
    streamed: bool
    estimated_tokens: int
    estimated_microdollars: int
    started_at: str
    client_ip: str | None = None
    existing_db_request_id: str | None = None
    generation_id: str = ""
    enqueue_monotonic_ns: int = field(default_factory=time.perf_counter_ns)
    enqueue_timestamp: str = field(default_factory=lambda: "")
    cancelled: asyncio.Event = field(
        default_factory=asyncio.Event,
        hash=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Validate the local fields required by the persistence schema."""
        if not self.proxy_request_id:
            raise DispatchValidationError("proxy_request_id must be non-empty")
        if self.attempt_number < 1:
            raise DispatchValidationError(
                f"attempt_number must be >= 1, got {self.attempt_number}"
            )
        if not self.account_name:
            raise DispatchValidationError("account_name must be non-empty")
        if self.account_id < 1:
            raise DispatchValidationError(
                f"account_id must be positive, got {self.account_id}"
            )
        if not self.provider_id:
            raise DispatchValidationError("provider_id must be non-empty")
        if not self.model_id:
            raise DispatchValidationError("model_id must be non-empty")
        if not self.protocol:
            raise DispatchValidationError("protocol must be non-empty")
        if self.estimated_tokens < 0:
            raise DispatchValidationError("estimated_tokens must be non-negative")
        if self.estimated_microdollars < 0:
            raise DispatchValidationError("estimated_microdollars must be non-negative")
        if self.attempt_number > 1 and not self.existing_db_request_id:
            raise DispatchValidationError(
                "attempt_number > 1 requires existing_db_request_id"
            )


@dataclass(frozen=True, slots=True)
class PersistedDispatchResult:
    """Result returned after a dispatch intent is durably committed.

    Carries the durable IDs and batch metadata back to the coordinator
    so it can publish runtime state and proceed with upstream dispatch.
    """

    db_request_id: str
    reservation_id: str
    attempt_id: int
    attempt_number: int
    batch_id: int
    batch_size: int
    commit_timestamp: str = ""
    queue_wait_ms: float = 0.0
    transaction_ms: float = 0.0

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Validate the durable identity before it crosses the boundary."""
        if not self.db_request_id:
            raise DispatchValidationError("db_request_id must be non-empty")
        if not self.reservation_id:
            raise DispatchValidationError("reservation_id must be non-empty")
        if isinstance(self.attempt_id, bool) or self.attempt_id < 1:
            raise DispatchValidationError(
                f"attempt_id must be positive, got {self.attempt_id}"
            )
