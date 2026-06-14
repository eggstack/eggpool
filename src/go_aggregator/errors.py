"""Exception hierarchy for the aggregator."""


class AggregatorError(Exception):
    """Base exception for all aggregator errors."""


class ConfigError(AggregatorError):
    """Raised for invalid or missing configuration."""


class DatabaseError(AggregatorError):
    """Raised for database-related failures."""


class UpstreamError(AggregatorError):
    """Base exception for upstream API errors."""


class AuthenticationError(UpstreamError):
    """Raised when an upstream rejects our credentials."""


class QuotaExhaustedError(UpstreamError):
    """Raised when an upstream account has exhausted its quota."""


class RateLimitError(UpstreamError):
    """Raised when we are rate-limited by an upstream."""


class ModelUnavailableError(UpstreamError):
    """Raised when the requested model is not available upstream."""


class ProxyError(AggregatorError):
    """Raised for general proxy/transport errors."""
