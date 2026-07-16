"""Payload size estimation utilities for hot-path optimization."""

from __future__ import annotations


def estimate_padded_size(base_size: int, estimated_expansion: int) -> int:
    """Estimate transformed payload size without allocating synthetic padding.

    Returns the estimated total size in bytes. Use this instead of
    constructing ``b"\\x00" * padding`` for context limit checks.
    """
    return base_size + max(0, estimated_expansion)
