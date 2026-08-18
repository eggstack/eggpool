"""Protocol-aware classification of clean upstream stream exhaustion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from eggpool.proxy.sse_observer import StreamCompletionSnapshot

CompletionPolicy = Literal["strict", "compatible", "permissive_observe"]
# Plan 144 (E2): ``terminal_failure`` and ``terminal_incomplete`` are
# distinct non-success classifications for Responses provider-level
# terminal events (``response.failed`` and ``response.incomplete``).
EOFClassification = Literal[
    "complete",
    "empty_eof",
    "premature_eof",
    "malformed_eof",
    "compatibility_eof",
    "terminal_failure",
    "terminal_incomplete",
]


@dataclass(frozen=True, slots=True)
class StreamEOFDecision:
    """The single decision produced when an upstream iterator reaches EOF."""

    classification: EOFClassification
    downstream_started: bool


def classify_stream_eof(
    *,
    protocol: str,
    policy: CompletionPolicy,
    snapshot: StreamCompletionSnapshot,
    downstream_started: bool,
) -> StreamEOFDecision:
    """Classify EOF from upstream protocol evidence and response state.

    ``policy`` is selected for the provider that owns the upstream attempt;
    absence of a marker is never globally treated as success.

    Plan 144 (E2): the classifier distinguishes Responses terminal
    events by kind.  ``responses_completed`` is the sole success;
    ``responses_failed`` and ``responses_incomplete`` are terminal
    non-success outcomes.
    """
    del protocol  # Reserved for protocol-specific policy extensions.
    if snapshot.saw_terminal_event:
        if snapshot.terminal_kind == "responses_completed":
            classification: EOFClassification = "complete"
        elif snapshot.terminal_kind == "responses_failed":
            classification = "terminal_failure"
        elif snapshot.terminal_kind == "responses_incomplete":
            classification = "terminal_incomplete"
        elif snapshot.terminal_kind in ("openai_done", "anthropic_message_stop"):
            classification = "complete"
        else:
            # Unknown terminal kind — treat as malformed if parser errors
            # occurred, otherwise assume complete (conservative).
            classification = (
                "malformed_eof" if snapshot.parser_error_count else "complete"
            )
    elif snapshot.incomplete_frame_at_eof or snapshot.parser_error_count:
        classification = "malformed_eof"
    elif not snapshot.saw_payload:
        classification = "empty_eof"
    elif (
        policy in {"compatible", "permissive_observe"} and snapshot.saw_usage_completion
    ):
        classification = "compatibility_eof"
    else:
        classification = "premature_eof"
    return StreamEOFDecision(
        classification=classification,
        downstream_started=downstream_started,
    )
