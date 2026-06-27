"""Tests for ToolCallIdMap."""

from __future__ import annotations

from eggpool.transcoder.ids import ToolCallIdMap


def test_register_and_lookup() -> None:
    m = ToolCallIdMap()
    m.register("c1", "u1")
    assert m.to_upstream("c1") == "u1"
    assert m.to_client("u1") == "c1"


def test_missing_keys_return_none() -> None:
    m = ToolCallIdMap()
    assert m.to_upstream("missing") is None
    assert m.to_client("missing") is None


def test_generate_upstream_id() -> None:
    m = ToolCallIdMap()
    id1 = m.generate_upstream_id()
    id2 = m.generate_upstream_id()
    assert id1 != id2
    assert id1.startswith("tcu_")
    assert id2.startswith("tcu_")


def test_len() -> None:
    m = ToolCallIdMap()
    assert len(m) == 0
    m.register("c1", "u1")
    assert len(m) == 1
    m.register("c2", "u2")
    assert len(m) == 2


def test_bool() -> None:
    m = ToolCallIdMap()
    assert not m
    m.register("c1", "u1")
    assert m
