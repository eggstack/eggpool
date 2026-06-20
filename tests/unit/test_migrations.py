"""Unit tests for migration statement parsing."""

from __future__ import annotations

from eggpool.db.migrations import _split_statements


def test_split_statements_ignores_semicolon_in_line_comment() -> None:
    sql = "-- explain the migration; this is not SQL\nCREATE TABLE example (id INT);"

    statements = _split_statements(sql)

    assert len(statements) == 1
    assert "CREATE TABLE example" in statements[0]


def test_split_statements_preserves_semicolon_in_string() -> None:
    statements = _split_statements("INSERT INTO example VALUES ('one;two'); SELECT 1;")

    assert len(statements) == 2
    assert "'one;two'" in statements[0]


def test_split_statements_keeps_trigger_body_together() -> None:
    sql = """
    CREATE TRIGGER update_example AFTER UPDATE ON example
    BEGIN
        INSERT INTO audit VALUES (NEW.id);
        UPDATE counters SET value = value + 1;
    END;
    SELECT 1;
    """

    statements = _split_statements(sql)

    assert len(statements) == 2
    assert "INSERT INTO audit" in statements[0]
    assert "UPDATE counters" in statements[0]


def test_split_statements_drops_comment_only_tail() -> None:
    assert _split_statements("-- comment only; still only a comment") == []
