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


def test_generate_anthropic_id() -> None:
    m = ToolCallIdMap()
    id1 = m.generate_anthropic_id()
    id2 = m.generate_anthropic_id()
    assert id1 != id2
    assert id1.startswith("toolu_")
    assert id2.startswith("toolu_")
    assert len(id1) == len("toolu_") + 24
    assert len(id2) == len("toolu_") + 24
    suffix1 = id1[len("toolu_") :]
    suffix2 = id2[len("toolu_") :]
    assert all(c in "0123456789abcdef" for c in suffix1)
    assert all(c in "0123456789abcdef" for c in suffix2)


def test_generate_upstream_id_is_alias_for_anthropic() -> None:
    m = ToolCallIdMap()
    upstream_id = m.generate_upstream_id()
    assert upstream_id.startswith("toolu_")
    assert len(upstream_id) == len("toolu_") + 24


def test_generate_openai_id() -> None:
    m = ToolCallIdMap()
    id1 = m.generate_openai_id()
    id2 = m.generate_openai_id()
    assert id1 != id2
    assert id1.startswith("call_")
    assert id2.startswith("call_")
    assert len(id1) == len("call_") + 24


def test_generators_do_not_collide() -> None:
    m = ToolCallIdMap()
    seen: set[str] = set()
    for _ in range(1000):
        seen.add(m.generate_anthropic_id())
        seen.add(m.generate_openai_id())
    assert len(seen) == 2000


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
