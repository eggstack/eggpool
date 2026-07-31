"""Canonical durable terminal-state vocabulary.

The request and attempt tables have different state representations.  Request
rows store the terminal status directly; attempt rows use ``completed_at`` as
the durable terminal marker.  Keeping the vocabularies together prevents
recovery from silently drifting away from the values written by finalizers.
"""

from __future__ import annotations

REQUEST_TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "client_error",
        "cancelled",
        "error",
        "interrupted",
        # Historical rows written by older releases remain terminal.  They
        # are compatibility values, not new values for production writers.
        "failed",
        "client_disconnected",
    }
)

REQUEST_PENDING_STATUSES = frozenset({"pending", "selected", "streaming"})

# These are the semantic values used by attempt finalization callers.  The
# current schema records terminality in ``completed_at``; this set is useful
# to callers and remains compatible with installations that expose a status
# projection for attempts.
ATTEMPT_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "error"})

RESERVATION_TERMINAL_STATUSES = frozenset({"released", "expired"})


def is_request_terminal(status: object) -> bool:
    """Return whether *status* is one of the statuses production writes."""

    return isinstance(status, str) and status in REQUEST_TERMINAL_STATUSES
