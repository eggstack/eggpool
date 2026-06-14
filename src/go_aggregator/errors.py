"""Exception hierarchy for the aggregator."""


class AggregatorError(Exception):
    """Base exception for all aggregator errors."""


class ConfigError(AggregatorError):
    """Raised for invalid or missing configuration."""


class DatabaseError(AggregatorError):
    """Raised for database-related failures."""


class UpstreamError(AggregatorError):
    """Base exception for upstream API errors."""

    def __init__(self, message: str = "", *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


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
