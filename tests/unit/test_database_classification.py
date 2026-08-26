"""Tests for database operation diagnostics classification."""

from __future__ import annotations

from eggpool.db.connection import _classify_op_kind


def test_classify_cte_by_outer_statement() -> None:
    assert (
        _classify_op_kind(
            "WITH ranked AS (SELECT id FROM accounts) SELECT id FROM ranked"
        )
        == "select"
    )
    assert (
        _classify_op_kind(
            "WITH ranked AS (SELECT id FROM accounts) "
            "UPDATE accounts SET weight = 2 WHERE id IN (SELECT id FROM ranked)"
        )
        == "update"
    )
