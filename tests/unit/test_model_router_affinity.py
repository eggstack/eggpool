"""Contracts for process-local sticky model-router affinity."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from eggpool.model_router.affinity import (
    ModelRouterAffinity,
    automatic_session_identity,
    session_identity_from_header,
)
from eggpool.model_router.config import ModelRouterConfig
from eggpool.model_router.registry import compile_model_router
from eggpool.model_router.selector import ModelSelection
from eggpool.wire.ir import canonical_request_from_mapping


def _router(**overrides: Any):
    values: dict[str, Any] = {
        "selector_model": "selector/local",
        "default_model": "model-default",
        "routes": {
            "default": {"model": "model-default", "description": "Default"},
            "fast": {"model": "model-fast", "description": "Fast"},
        },
    }
    values.update(overrides)
    return compile_model_router(
        "virtual",
        ModelRouterConfig.model_validate(values),
    )


def _selection(router: Any, *, source: str = "selector", route_id: str = "1"):
    route = router.route_by_id[route_id]
    return ModelSelection(
        virtual_model=router.virtual_model,
        route_id=route.route_id,
        route_label=route.label,
        concrete_model=route.model,
        source=source,  # type: ignore[arg-type]
        selector_attempts=1,
        selector_latency_ms=0.1,
    )


def _canonical(payload: dict[str, object], surface: str = "chat_completions"):
    return canonical_request_from_mapping(
        payload,
        client_surface=surface,  # type: ignore[arg-type]
        protocol="openai",
    )


def test_explicit_session_is_hashed_and_invalid_values_are_unavailable() -> None:
    identity = session_identity_from_header("conversation-42")
    assert identity is not None
    assert identity.source == "explicit_session"
    assert identity.digest != b"conversation-42"
    assert "conversation-42" not in repr(identity)
    assert session_identity_from_header("x" * 513) is None
    assert session_identity_from_header("bad\nvalue") is None
    assert session_identity_from_header("other-conversation") != identity


def test_automatic_identity_uses_initial_text_only_and_omits_responses() -> None:
    first = _canonical(
        {
            "model": "virtual",
            "messages": [
                {"role": "system", "content": "stable instruction"},
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "later question"},
            ],
        }
    )
    grown = _canonical(
        {
            "model": "virtual",
            "messages": [
                {"role": "system", "content": "stable instruction"},
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "different answer"},
                {"role": "user", "content": "different later question"},
            ],
        }
    )

    first_identity = automatic_session_identity(
        first, client_surface="chat_completions"
    )
    grown_identity = automatic_session_identity(
        grown,
        client_surface="chat_completions",
    )
    assert first_identity == grown_identity
    assert (
        automatic_session_identity(
            _canonical(
                {
                    "model": "virtual",
                    "messages": [{"role": "user", "content": "other"}],
                }
            ),
            client_surface="chat_completions",
        )
        != first_identity
    )
    assert (
        automatic_session_identity(
            _canonical({"model": "virtual", "input": "one response"}, "responses"),
            client_surface="responses",
        )
        is None
    )


@pytest.mark.asyncio
async def test_ttl_and_lru_are_bounded() -> None:
    now = [10.0]
    cache = ModelRouterAffinity(max_entries=2, clock=lambda: now[0])
    router = _router()
    identity_a = session_identity_from_header("a")
    identity_b = session_identity_from_header("b")
    identity_c = session_identity_from_header("c")
    assert identity_a and identity_b and identity_c

    async def resolve(identity: Any) -> Any:
        async def selector() -> Any:
            return _selection(router, source="default", route_id="0")

        return await cache.resolve(
            router,
            identity,
            selector,
        )

    await resolve(identity_a)
    await resolve(identity_b)
    await resolve(identity_a)  # Make A most recently used.
    await resolve(identity_c)
    assert cache.entry_count == 2
    assert cache.stats.evictions == 1
    assert cache.stats.hits == 1

    now[0] += router.affinity_ttl_s + 1
    assert (await resolve(identity_a)).cache_hit is False
    assert cache.stats.expirations >= 1


@pytest.mark.asyncio
async def test_concurrent_miss_single_flights_and_releases_state() -> None:
    cache = ModelRouterAffinity()
    router = _router()
    identity = session_identity_from_header("same")
    assert identity is not None
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def selector() -> Any:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return _selection(router)

    tasks = [
        asyncio.create_task(cache.resolve(router, identity, selector)) for _ in range(8)
    ]
    await started.wait()
    assert calls == 1
    assert cache.single_flight_count == 1
    release.set()
    results = await asyncio.gather(*tasks)
    assert calls == 1
    assert {result.decision.concrete_model for result in results} == {"model-fast"}
    assert cache.single_flight_count == 0
    assert cache.stats.single_flight_joins == 7


@pytest.mark.asyncio
async def test_cancelled_leader_does_not_leave_followers_waiting() -> None:
    cache = ModelRouterAffinity()
    router = _router()
    identity = session_identity_from_header("cancelled-leader")
    assert identity is not None
    started = asyncio.Event()
    calls = 0

    async def selector() -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await asyncio.Event().wait()
        return _selection(router)

    leader = asyncio.create_task(cache.resolve(router, identity, selector))
    await started.wait()
    follower = asyncio.create_task(cache.resolve(router, identity, selector))
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader
    result = await asyncio.wait_for(follower, timeout=1.0)
    assert result.decision.concrete_model == "model-fast"
    assert calls == 2
    assert cache.single_flight_count == 0


@pytest.mark.asyncio
async def test_router_fingerprint_partitions_decisions() -> None:
    cache = ModelRouterAffinity()
    identity = session_identity_from_header("same-session")
    assert identity is not None
    first = _router()
    second = _router(default_model="model-fast")
    calls = 0

    async def select_first() -> Any:
        nonlocal calls
        calls += 1
        return _selection(first)

    async def select_second() -> Any:
        nonlocal calls
        calls += 1
        return _selection(second, source="default")

    await cache.resolve(first, identity, select_first)
    changed = await cache.resolve(second, identity, select_second)
    assert changed.cache_hit is False
    assert changed.decision.source == "default"
    assert calls == 2
