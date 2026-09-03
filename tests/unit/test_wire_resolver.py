"""Deterministic contracts for runtime wire preference governance."""

from __future__ import annotations

import asyncio
import contextlib

from eggpool.models.config import (
    ModelWirePreference,
    ProviderAuthConfig,
    ProviderConfig,
    ProviderWireSurfaceConfig,
)
from eggpool.wire.registry import resolve_provider_wire_profiles
from eggpool.wire.resolver import WireProfileResolver


def _provider(*, with_preference: bool = False) -> ProviderConfig:
    return ProviderConfig(
        id="synthetic",
        base_url="https://example.test/v1",
        protocols=["openai"],
        auth=ProviderAuthConfig(mode="bearer"),
        wire_surfaces={
            "openai_chat_completions": ProviderWireSurfaceConfig(
                path_template="/a", priority=10
            ),
            "openai_responses": ProviderWireSurfaceConfig(
                path_template="/b", priority=20
            ),
            "gemini_interactions": ProviderWireSurfaceConfig(
                path_template="/c", priority=30
            ),
        },
        model_wire=(
            {
                "model-x": ModelWirePreference(
                    preferred_surface="openai_chat_completions"
                )
            }
            if with_preference
            else {}
        ),
    )


def test_success_learns_preference_and_keeps_one_request_steady_state() -> None:
    provider = _provider(with_preference=True)
    profiles = resolve_provider_wire_profiles(provider)
    resolver = WireProfileResolver()

    first = resolver.resolve(provider, "model-x", profiles=profiles, now_monotonic=10.0)
    assert [profile.surface for profile in first.candidates] == [
        "openai_chat_completions",
        "openai_responses",
        "gemini_interactions",
    ]

    resolver.record_success(
        provider.id,
        "model-x",
        first.candidate_fingerprint,
        "openai_responses",
        now_monotonic=11.0,
        now_epoch=101.0,
    )
    second = resolver.resolve(
        provider,
        "model-x",
        profiles=profiles,
        now_monotonic=12.0,
        now_epoch=102.0,
    )
    assert second.candidates[0].surface == "openai_responses"
    assert second.selected_source == "learned_runtime"


def test_operator_fixed_surface_is_the_only_candidate() -> None:
    provider = _provider(with_preference=True)
    provider.model_wire["model-x"] = ModelWirePreference(
        preferred_surface="openai_chat_completions", fixed=True
    )
    resolver = WireProfileResolver()
    resolution = resolver.resolve(provider, "model-x")
    assert [profile.surface for profile in resolution.candidates] == [
        "openai_chat_completions"
    ]
    assert resolution.fixed is True


def test_rejection_cooldown_omits_candidate_without_forcing_a_probe() -> None:
    provider = _provider(with_preference=True)
    resolver = WireProfileResolver()
    profiles = resolve_provider_wire_profiles(provider)
    resolution = resolver.resolve(
        provider, "model-x", profiles=profiles, now_monotonic=10.0
    )
    resolver.record_deterministic_rejection(
        provider.id,
        "model-x",
        resolution.candidate_fingerprint,
        "openai_chat_completions",
        rejection_class="endpoint_405",
        cooldown_s=30.0,
        now_monotonic=10.0,
    )
    next_resolution = resolver.resolve(
        provider, "model-x", profiles=profiles, now_monotonic=11.0
    )
    assert [profile.surface for profile in next_resolution.candidates] == [
        "openai_responses",
        "gemini_interactions",
    ]
    assert (
        resolver.is_suppressed(
            provider.id,
            "model-x",
            resolution.candidate_fingerprint,
            "openai_chat_completions",
            now_monotonic=39.0,
        )
        is True
    )
    assert (
        resolver.is_suppressed(
            provider.id,
            "model-x",
            resolution.candidate_fingerprint,
            "openai_chat_completions",
            now_monotonic=40.0,
        )
        is False
    )


def test_stale_learned_preference_uses_current_static_hint_without_probe() -> None:
    provider = _provider()
    profiles = resolve_provider_wire_profiles(provider)
    resolver = WireProfileResolver()
    initial = resolver.resolve(
        provider, "model-x", profiles=profiles, now_monotonic=0.0
    )
    resolver.record_success(
        provider.id,
        "model-x",
        initial.candidate_fingerprint,
        "openai_chat_completions",
        now_monotonic=1.0,
        now_epoch=1.0,
    )
    stale = resolver.resolve(
        provider,
        "model-x",
        profiles=profiles,
        bundled_hint="openai_responses",
        learned_preference_ttl_s=5.0,
        now_monotonic=10.0,
    )
    assert stale.candidates[0].surface == "openai_responses"
    assert stale.selected_source == "bundled_hint"


def test_structural_change_gets_new_cache_key_and_bound_is_enforced() -> None:
    provider = _provider()
    resolver = WireProfileResolver(cache_max_entries=1)
    profiles = resolve_provider_wire_profiles(provider)
    first = resolver.resolve(provider, "model-x", profiles=profiles)
    resolver.record_success(
        provider.id, "model-x", first.candidate_fingerprint, "openai_responses"
    )
    provider.wire_surfaces["openai_responses"].path_template = "/b-v2"
    changed_profiles = resolve_provider_wire_profiles(provider)
    changed = resolver.resolve(provider, "model-x", profiles=changed_profiles)
    assert changed.candidate_fingerprint != first.candidate_fingerprint
    assert changed.candidates[0].surface == "openai_chat_completions"
    resolver.record_success(
        provider.id,
        "other-model",
        changed.candidate_fingerprint,
        "openai_chat_completions",
    )
    assert resolver.snapshot()["cache_entries"] == 1


def test_fingerprint_tracks_provider_and_request_constraints() -> None:
    provider = _provider()
    profiles = resolve_provider_wire_profiles(provider)
    base = WireProfileResolver.candidate_fingerprint(provider, profiles)
    constrained = WireProfileResolver.candidate_fingerprint(
        provider,
        profiles,
        allowed_surfaces=("openai_responses",),
        metadata_surface="openai_responses",
        bundled_hint="openai_responses",
    )
    assert constrained != base

    provider.base_url = "https://changed.example.test/v1"
    changed_provider = WireProfileResolver.candidate_fingerprint(provider, profiles)
    assert changed_provider != base


def test_singleflight_shares_acceptance_decision() -> None:
    async def scenario() -> None:
        provider = _provider()
        resolver = WireProfileResolver()
        resolution = resolver.resolve(provider, "model-x")
        leader = await resolver.begin_negotiation(
            resolution, max_concurrent_per_provider=1, min_negotiation_interval_s=0
        )
        follower = await resolver.begin_negotiation(
            resolution, max_concurrent_per_provider=1, min_negotiation_interval_s=0
        )
        second_follower = await resolver.begin_negotiation(
            resolution, max_concurrent_per_provider=1, min_negotiation_interval_s=0
        )
        assert leader.is_leader is True
        assert follower.is_leader is False
        assert second_follower.is_leader is False
        async with leader:
            result = await leader.accept("openai_responses")
        assert result.accepted_surface == "openai_responses"
        assert (await follower.wait_for_acceptance()).result == "accepted"
        assert (await second_follower.wait_for_acceptance()).result == "accepted"
        assert resolver.snapshot()["inflight"] == 0

    asyncio.run(scenario())


def test_cancelled_waiting_leader_does_not_release_gate_capacity() -> None:
    async def scenario() -> None:
        provider = _provider()
        resolver = WireProfileResolver()
        first = await resolver.begin_negotiation(
            resolver.resolve(provider, "model-a"),
            max_concurrent_per_provider=1,
            min_negotiation_interval_s=0,
        )
        second = await resolver.begin_negotiation(
            resolver.resolve(provider, "model-b"),
            max_concurrent_per_provider=1,
            min_negotiation_interval_s=0,
        )
        third = await resolver.begin_negotiation(
            resolver.resolve(provider, "model-c"),
            max_concurrent_per_provider=1,
            min_negotiation_interval_s=0,
        )
        await first.__aenter__()
        second_task = asyncio.create_task(second.__aenter__())
        await asyncio.sleep(0)
        second_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await second_task

        third_task = asyncio.create_task(third.__aenter__())
        await asyncio.sleep(0)
        assert third_task.done() is False
        await first.finish(result="accepted", surface="openai_responses")
        await third_task
        await third.finish(result="accepted", surface="openai_responses")

    asyncio.run(scenario())


def test_cancelled_follower_does_not_cancel_shared_decision() -> None:
    async def scenario() -> None:
        provider = _provider()
        resolver = WireProfileResolver()
        resolution = resolver.resolve(provider, "model-x")
        leader = await resolver.begin_negotiation(
            resolution, max_concurrent_per_provider=1, min_negotiation_interval_s=0
        )
        cancelled_follower = await resolver.begin_negotiation(resolution)
        remaining_follower = await resolver.begin_negotiation(resolution)
        wait_task = asyncio.create_task(cancelled_follower.wait_for_acceptance())
        await asyncio.sleep(0)
        wait_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await wait_task
        assert leader.future.cancelled() is False
        assert remaining_follower.future.cancelled() is False
        await leader.accept("openai_responses")
        assert (await remaining_follower.wait_for_acceptance()).result == "accepted"
        assert resolver.snapshot()["inflight"] == 0

    asyncio.run(scenario())


def test_cancelled_leader_publishes_rejection_and_releases_owned_permit() -> None:
    async def scenario() -> None:
        provider = _provider()
        resolver = WireProfileResolver()
        resolution = resolver.resolve(provider, "model-x")
        leader = await resolver.begin_negotiation(
            resolution, max_concurrent_per_provider=1, min_negotiation_interval_s=0
        )
        follower = await resolver.begin_negotiation(resolution)
        with contextlib.suppress(asyncio.CancelledError):
            async with leader:
                raise asyncio.CancelledError

        decision = await follower.wait_for_acceptance()
        assert decision.result == "rejected"
        assert decision.accepted_surface is None
        assert resolver.snapshot()["inflight"] == 0
        next_leader = await resolver.begin_negotiation(
            resolver.resolve(provider, "model-y"),
            max_concurrent_per_provider=1,
            min_negotiation_interval_s=0,
        )
        await next_leader.__aenter__()
        await next_leader.finish(result="accepted", surface="openai_responses")

    asyncio.run(scenario())


def test_provider_gate_limits_only_negotiation_dispatches() -> None:
    async def scenario() -> None:
        provider = _provider()
        resolver = WireProfileResolver()
        first = resolver.resolve(provider, "model-a")
        second = resolver.resolve(provider, "model-b")
        leader_a = await resolver.begin_negotiation(
            first, max_concurrent_per_provider=1, min_negotiation_interval_s=0
        )
        leader_b = await resolver.begin_negotiation(
            second, max_concurrent_per_provider=1, min_negotiation_interval_s=0
        )
        active = 0
        maximum = 0

        async def run(handle: object) -> None:
            nonlocal active, maximum
            async with handle:  # type: ignore[attr-defined]
                active += 1
                maximum = max(maximum, active)
                await asyncio.sleep(0)
                active -= 1
                await handle.accept("openai_responses")  # type: ignore[attr-defined]

        await asyncio.gather(run(leader_a), run(leader_b))
        assert maximum == 1

    asyncio.run(scenario())


def test_rate_limit_stops_flight_and_throttles_the_next_one() -> None:
    async def scenario() -> None:
        provider = _provider()
        resolver = WireProfileResolver()
        resolution = resolver.resolve(provider, "model-x")
        leader = await resolver.begin_negotiation(
            resolution, min_negotiation_interval_s=0
        )
        follower = await resolver.begin_negotiation(resolution)
        async with leader:
            result = await leader.rate_limited(retry_after_s=120.0)
        assert result.result == "rate_limited"
        assert (await follower.wait_for_acceptance()).result == "rate_limited"
        next_leader = await resolver.begin_negotiation(
            resolution, min_negotiation_interval_s=0
        )
        assert next_leader.role == "throttled"

    asyncio.run(scenario())


def test_gate_occupancy_does_not_change_normal_resolution() -> None:
    async def scenario() -> None:
        provider = _provider()
        resolver = WireProfileResolver()
        resolution = resolver.resolve(provider, "model-x")
        leader = await resolver.begin_negotiation(
            resolution, max_concurrent_per_provider=1, min_negotiation_interval_s=0
        )
        await leader.__aenter__()
        normal = resolver.resolve(provider, "model-x")
        assert normal.preferred is not None
        assert normal.preferred.surface == resolution.preferred.surface
        await leader.finish(result="accepted", surface="openai_responses")

    asyncio.run(scenario())
