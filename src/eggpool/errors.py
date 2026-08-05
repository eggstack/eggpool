"""Exception hierarchy for the aggregator."""


class AggregatorError(Exception):
    """Base exception for all aggregator errors."""


class AcceptedFinalizationInvariantError(AggregatorError):
    """Raised when an accepted-finalization invariant is violated.

    Plan 020 Workstream B5: an unknown or invalid progress state cannot
    be converted into ``COMPLETED``.  This typed error is raised from
    :class:`AcceptedReloadFinalizationJob` when the dispatch table
    cannot resolve the current progress step.  The reload manager
    surfaces this error both inline and after the job's buffered
    attempt completes; the job is retained in the active registry and
    is retryable by the same code path that retries any other failed
    step.
    """

    def __init__(
        self,
        message: str = "",
        *,
        step: str | None = None,
        request_id: str | None = None,
        generation_id: int | None = None,
    ) -> None:
        super().__init__(message)
        self.step = step
        self.request_id = request_id
        self.generation_id = generation_id


class ConfigError(AggregatorError):
    """Raised for invalid or missing configuration."""


class DatabaseError(AggregatorError):
    """Raised for database-related failures."""


class DatabaseTransactionOwnershipError(DatabaseError):
    """Raised when a task other than the transaction owner touches SQLite."""


class DatabaseCommitError(DatabaseError):
    """Raised when the SQLite COMMIT call itself fails.

    Attributes:
        rollback_attempted: True if a rollback was attempted after the
            commit failure.
        rollback_succeeded: True if the rollback completed successfully.
        transaction_still_active: True if ``in_transaction`` remains True
            after rollback attempt; None when indeterminate.
        connection_invalidated: True if the connection is in an unknown
            state and should not be reused.
        outcome: Categorical summary — ``"rolled_back"`` when the
            connection is clean, ``"indeterminate"`` otherwise.
    """

    def __init__(
        self,
        message: str = "",
        *,
        rollback_attempted: bool = False,
        rollback_succeeded: bool = False,
        transaction_still_active: bool | None = None,
        connection_invalidated: bool = False,
        outcome: str = "indeterminate",
    ) -> None:
        super().__init__(message)
        self.rollback_attempted = rollback_attempted
        self.rollback_succeeded = rollback_succeeded
        self.transaction_still_active = transaction_still_active
        self.connection_invalidated = connection_invalidated
        self.outcome = outcome


class DatabaseConnectionInvalidatedError(DatabaseError):
    """Raised when the database connection has been invalidated.

    The connection was detached and closed after an indeterminate
    commit failure.  A new ``connect()`` call is required before
    future database access.
    """


class DatabaseRollbackError(DatabaseError):
    """Raised when the SQLite ROLLBACK call itself fails.

    Plan 027 Workstream D — ``transaction()`` distinguishes a
    rollback failure from a commit failure so callers see the
    original cause of the orphaned-transaction problem.  The
    connection is detached and closed before this error is raised;
    subsequent ``transaction()`` calls raise
    :class:`DatabaseConnectionInvalidatedError`.

    Attributes:
        rollback_attempted: True if a rollback was attempted.
        rollback_succeeded: True if the rollback completed successfully.
        transaction_still_active: True if ``in_transaction`` remains True
            after the rollback attempt; ``None`` when indeterminate.
        connection_invalidated: True if the connection has been
            detached and closed.
        original_exception: The exception raised by ``rollback()``.
    """

    def __init__(
        self,
        message: str = "",
        *,
        rollback_attempted: bool = False,
        rollback_succeeded: bool = False,
        transaction_still_active: bool | None = None,
        connection_invalidated: bool = False,
        original_exception: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.rollback_attempted = rollback_attempted
        self.rollback_succeeded = rollback_succeeded
        self.transaction_still_active = transaction_still_active
        self.connection_invalidated = connection_invalidated
        self.original_exception = original_exception


class UpstreamError(AggregatorError):
    """Base exception for upstream API errors."""

    def __init__(self, message: str = "", *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TemporaryUpstreamError(UpstreamError):
    """Raised for temporary upstream errors (502, 503, 504)."""


class TransientUpstreamError(UpstreamError):
    """Raised for transient upstream errors (retries may succeed)."""


class AuthenticationError(UpstreamError):
    """Raised when an upstream rejects our credentials."""


class QuotaExhaustedError(UpstreamError):
    """Raised when an upstream account has exhausted its quota."""


class RateLimitError(UpstreamError):
    """Raised when we are rate-limited by an upstream."""

    def __init__(
        self,
        message: str = "",
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code)
        self.retry_after = retry_after


class ModelUnavailableError(UpstreamError):
    """Raised when the requested model is not available upstream."""


class ProxyError(AggregatorError):
    """Raised for general proxy/transport errors."""


class PrematureStreamEOFError(ProxyError):
    """Raised when a streaming upstream closes before protocol completion."""

    def __init__(self, classification: str, *, request_id: str | None = None) -> None:
        self.classification = classification
        self.request_id = request_id
        super().__init__(
            f"Upstream stream ended without a valid terminal event ({classification})"
        )


class ModelNotFoundError(AggregatorError):
    """Raised when the requested model does not exist (404)."""

    def __init__(self, model_id: str = "") -> None:
        self.model_id = model_id
        super().__init__(f"Model {model_id!r} not found")


class NoEligibleAccountError(AggregatorError):
    """Raised when no account can serve the request (503)."""


class CatalogUnavailableError(AggregatorError):
    """Raised when the model catalog is not available (503)."""


class AuthenticationUnavailableError(AggregatorError):
    """Raised when upstream credentials cannot be loaded (503)."""


class UpstreamExhaustedError(AggregatorError):
    """Raised when all upstream attempts have been exhausted (502)."""


class AccountSuspendedError(AggregatorError):
    """Raised when an account has been suspended (503)."""


class RequestTooLargeError(AggregatorError):
    """Raised when a request body exceeds the configured limit."""


class ModelInfoSourceFetchError(AggregatorError):
    """Raised when a model-info source fetch fails (network, HTTP, parse)."""


class CapabilityError(AggregatorError):
    """Raised when a request requires a capability the model does not support."""

    def __init__(
        self,
        *,
        model_id: str,
        capability: str,
        requested_fields: list[str],
        message: str,
    ) -> None:
        self.model_id = model_id
        self.capability = capability
        self.requested_fields = requested_fields
        super().__init__(message)


class ContextLimitExceededError(AggregatorError):
    """Raised when estimated request context exceeds the configured limit."""

    def __init__(
        self,
        *,
        model_id: str,
        estimated_input_tokens: int,
        requested_output_tokens: int | None,
        max_context_tokens: int | None,
        max_input_tokens: int | None,
        max_output_tokens: int | None = None,
    ) -> None:
        self.model_id = model_id
        self.estimated_input_tokens = estimated_input_tokens
        self.requested_output_tokens = requested_output_tokens
        self.max_context_tokens = max_context_tokens
        self.max_input_tokens = max_input_tokens
        self.max_output_tokens = max_output_tokens
        parts = [f"model {model_id!r}"]
        if max_context_tokens is not None:
            parts.append(f"max context {max_context_tokens}")
        if max_input_tokens is not None:
            parts.append(f"max input {max_input_tokens}")
        if max_output_tokens is not None:
            parts.append(f"max output {max_output_tokens}")
        parts.append(f"estimated input {estimated_input_tokens}")
        if requested_output_tokens is not None:
            parts.append(f"requested output {requested_output_tokens}")
        super().__init__("Context limit exceeded: " + ", ".join(parts))
