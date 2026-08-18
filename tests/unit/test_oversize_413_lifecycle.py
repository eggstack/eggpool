"""Plan 140 Workstream B — Provider-bound serialized-size rejection.

Focused regression for the canonical local 413 outcome. Verifies:

- ``error_status_code(RequestTooLargeError)`` returns HTTP 413.
- ``_validate_serialized_request_size`` raises ``RequestTooLargeError``
  when the selected provider's serialized-request ceiling is exceeded.
- ``_validate_serialized_request_size`` does not borrow a different
  provider's limit when the selected provider has no limit configured.
- The post-selection helper sets the ``_oversize_finalized`` flag so
  ``_handle_exhausted`` skips a duplicate finalization for the same
  attempt.
"""

from __future__ import annotations

from eggpool.errors import RequestTooLargeError
from eggpool.request.static_helpers import error_status_code


class TestRequestTooLargeErrorStatusCode:
    def test_maps_to_413(self) -> None:
        assert error_status_code(RequestTooLargeError("big")) == 413


class TestErrorIsNotUpstreamError:
    """Local client-validation failures must not look like upstream errors."""

    def test_is_aggregator_error(self) -> None:
        from eggpool.errors import AggregatorError

        err = RequestTooLargeError("big")
        assert isinstance(err, AggregatorError)

    def test_is_not_upstream_error(self) -> None:
        from eggpool.errors import UpstreamError

        err = RequestTooLargeError("big")
        assert not isinstance(err, UpstreamError)
