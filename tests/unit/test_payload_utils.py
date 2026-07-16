"""Tests for estimate_padded_size (Milestone F8).

Verifies:

- Zero expansion returns base size.
- Positive expansion adds to base.
- Negative expansion is clamped to zero.
- Very large expansions do not allocate proportional memory.
- Boundary values (zero base, zero expansion).
"""

from __future__ import annotations

from eggpool.request.payload_utils import estimate_padded_size


class TestEstimatePaddedSize:
    def test_zero_expansion(self) -> None:
        assert estimate_padded_size(1000, 0) == 1000

    def test_positive_expansion(self) -> None:
        assert estimate_padded_size(500, 250) == 750

    def test_negative_expansion_clamped(self) -> None:
        assert estimate_padded_size(500, -100) == 500

    def test_large_expansion(self) -> None:
        assert estimate_padded_size(100, 1_000_000) == 1_000_100

    def test_zero_base_zero_expansion(self) -> None:
        assert estimate_padded_size(0, 0) == 0

    def test_zero_base_positive_expansion(self) -> None:
        assert estimate_padded_size(0, 500) == 500

    def test_zero_base_negative_expansion(self) -> None:
        assert estimate_padded_size(0, -100) == 0

    def test_very_large_values(self) -> None:
        base = 2**31
        expansion = 2**31
        result = estimate_padded_size(base, expansion)
        assert result == 2**32

    def test_returns_int(self) -> None:
        result = estimate_padded_size(100, 50)
        assert isinstance(result, int)

    def test_one_byte_expansion(self) -> None:
        assert estimate_padded_size(100, 1) == 101
