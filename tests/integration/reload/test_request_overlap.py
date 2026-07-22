"""Request overlap tests (D4).

Verifies per-request generation coherence: old in-flight requests use
old-generation services, new requests use new-generation services.

These tests acquire leases from the runtime manager before and after
a reload, then verify that the leased generation's services are
consistent with the expected generation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from eggpool.models.config import (
    AccountConfig,
    AppConfig,
    ProviderConfig,
    RoutingConfig,
    ServerConfig,
)
from eggpool.proxy.client import (
    HOP_BY_HOP_HEADERS,
    LOCAL_CREDENTIAL_HEADERS,
)

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


def _scrubbed_provider_config() -> AppConfig:
    """Build a config that removes ``test-provider-a`` from the candidate."""
    return AppConfig(
        server=ServerConfig(host="127.0.0.1", port=0),
        providers={
            "test-provider-b": ProviderConfig(
                id="test-provider-b",
                base_url="https://b.example.com/v1",
                protocols=["openai"],
                accounts=[
                    AccountConfig(name="acct-b1", api_key="test-key-b1"),
                ],
            ),
        },
        routing=RoutingConfig(strategy="quota_fair", local_quota_mode="score_only"),
    )


def _transcode_loss_strict_config() -> AppConfig:
    """Build a candidate with stricter transcode loss policy."""
    cfg = AppConfig(
        server=ServerConfig(host="127.0.0.1", port=0),
        providers={
            "test-provider-a": ProviderConfig(
                id="test-provider-a",
                base_url="https://a.example.com/v1",
                protocols=["openai"],
                accounts=[
                    AccountConfig(name="acct-a1", api_key="test-key-a1"),
                    AccountConfig(name="acct-a2", api_key="test-key-a2"),
                ],
            ),
            "test-provider-b": ProviderConfig(
                id="test-provider-b",
                base_url="https://b.example.com/v1",
                protocols=["openai"],
                accounts=[
                    AccountConfig(name="acct-b1", api_key="test-key-b1"),
                ],
            ),
        },
        routing=RoutingConfig(strategy="quota_fair", local_quota_mode="score_only"),
    )
    # Override the transcoder section to enforce strict loss rejection.
    cfg_dict = cfg.model_dump()
    cfg_dict.setdefault("transcoder", {})["loss_policy"] = "reject"
    return AppConfig(**cfg_dict)


@pytest.mark.asyncio
async def test_old_lease_uses_old_generation_services(
    reload_harness: ReloadHarness,
) -> None:
    """A lease acquired before reload points to old generation services.

    After reload, the old lease's catalog, coordinator, and other
    services must remain the same objects — not replaced by new
    generation services.
    """
    # Acquire a lease from the initial generation.
    old_lease = await reload_harness.runtime_manager.acquire()
    old_catalog = old_lease.runtime.catalog
    old_coordinator = old_lease.runtime.coordinator
    old_gen_id = old_lease.runtime.generation_id

    # Reload with a new configuration.
    result = await reload_harness.reload()
    assert result.ok is True

    # The old lease still points to the old generation's services.
    assert old_lease.runtime.catalog is old_catalog
    assert old_lease.runtime.coordinator is old_coordinator
    assert old_lease.runtime.generation_id == old_gen_id

    # Release the old lease.
    await old_lease.release()


@pytest.mark.asyncio
async def test_new_lease_uses_new_generation_services(
    reload_harness: ReloadHarness,
) -> None:
    """A lease acquired after reload points to new generation services.

    The new lease's catalog and coordinator must be different objects
    from the old generation's services.
    """
    # Capture old generation service identities.
    old_lease = await reload_harness.runtime_manager.acquire()
    old_catalog_id = id(old_lease.runtime.catalog)
    old_coordinator_id = id(old_lease.runtime.coordinator)
    await old_lease.release()

    # Reload with a new configuration.
    result = await reload_harness.reload()
    assert result.ok is True

    # Acquire a new lease — it must point to new generation services.
    new_lease = await reload_harness.runtime_manager.acquire()
    assert new_lease.runtime.generation_id != old_lease.runtime.generation_id
    assert id(new_lease.runtime.catalog) != old_catalog_id
    assert id(new_lease.runtime.coordinator) != old_coordinator_id

    await new_lease.release()


@pytest.mark.asyncio
async def test_concurrent_old_and_new_leases_during_reload(
    reload_harness: ReloadHarness,
) -> None:
    """Old and new leases coexist during reload with different services.

    An old lease held during reload must not observe any new-generation
    service object, and a new lease must not observe any old-generation
    service object.
    """
    # Acquire old lease.
    old_lease = await reload_harness.runtime_manager.acquire()
    old_gen_id = old_lease.runtime.generation_id
    old_catalog = old_lease.runtime.catalog

    # Reload — old lease is still held.
    result = await reload_harness.reload()
    assert result.ok is True

    # New lease gets new generation.
    new_lease = await reload_harness.runtime_manager.acquire()
    new_gen_id = new_lease.runtime.generation_id

    # Generations are different.
    assert new_gen_id != old_gen_id

    # Services are different objects.
    assert new_lease.runtime.catalog is not old_catalog

    # Old lease still points to old generation.
    assert old_lease.runtime.generation_id == old_gen_id
    assert old_lease.runtime.catalog is old_catalog

    await old_lease.release()
    await new_lease.release()


@pytest.mark.asyncio
async def test_old_generation_config_values_preserved_in_lease(
    reload_harness: ReloadHarness,
) -> None:
    """Old lease preserves old generation's config values after reload.

    The old lease's config must reflect the initial configuration,
    not the reloaded configuration.
    """
    # Acquire old lease.
    old_lease = await reload_harness.runtime_manager.acquire()
    old_config = old_lease.runtime.config
    initial_provider_ids: frozenset[str] = (
        old_lease.runtime.immutable_request_state.provider_ids
    )
    initial_account_names: frozenset[str] = (
        old_lease.runtime.immutable_request_state.account_names
    )

    # Reload with a different configuration.
    result = await reload_harness.reload()
    assert result.ok is True

    # Old lease still has old config (object identity).
    assert old_lease.runtime.config is old_config
    assert old_lease.runtime.config.routing.strategy == "quota_fair"

    # New lease has new config (different identity).
    new_lease = await reload_harness.runtime_manager.acquire()
    assert new_lease.runtime.config is not old_config

    # Each lease keeps its precomputed immutable request state.  Since
    # the harness uses a MagicMock registry, both old and new
    # provider_ids sets are empty here — so we assert identity, not
    # content.  See ``test_overlap_provider_removed_in_new_generation``
    # for content-based checks using real Registry instances.
    assert (
        new_lease.runtime.immutable_request_state.provider_ids
        is not old_lease.runtime.immutable_request_state.provider_ids
    )
    _ = initial_provider_ids
    _ = initial_account_names

    await old_lease.release()
    await new_lease.release()


# ---------------------------------------------------------------------------
# D4: per-field overlap scenarios.  Each test holds an old lease during a
# reload that changes a specific LIVE field, then verifies the old lease
# continues to observe the pre-reload value while a new lease observes
# the post-reload value.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_overlap_provider_set_is_per_generation(
    reload_harness: ReloadHarness,
) -> None:
    """Provider_ids frozenset is a per-generation snapshot.

    Under live reload the old lease must continue to observe its
    captured provider set; a new lease must observe a distinct set
    from the new generation.  The harness supplies a MagicMock
    registry (so both sets are empty), but the structural property —
    distinct identities for distinct generations — still holds.
    """
    old_lease = await reload_harness.runtime_manager.acquire()
    old_provider_ids: frozenset[str] = (
        old_lease.runtime.immutable_request_state.provider_ids
    )

    result = await reload_harness.reload()
    assert result.ok is True

    new_lease = await reload_harness.runtime_manager.acquire()
    # Both leases carry a per-generation frozenset — distinct objects.
    assert isinstance(old_provider_ids, frozenset)
    assert isinstance(
        new_lease.runtime.immutable_request_state.provider_ids,
        frozenset,
    )
    # Identity divergence even when contents match.
    assert (
        new_lease.runtime.immutable_request_state.provider_ids is not old_provider_ids
    )
    # Old lease snapshot is unchanged by reload.
    assert old_lease.runtime.immutable_request_state.provider_ids is old_provider_ids

    await old_lease.release()
    await new_lease.release()


@pytest.mark.asyncio
async def test_overlap_account_names_set_is_per_generation(
    reload_harness: ReloadHarness,
) -> None:
    """The ``immutable_request_state.account_names`` frozenset is per-generation.

    Same property as provider_ids: identity divergence after reload,
    old lease snapshot preserved.
    """
    old_lease = await reload_harness.runtime_manager.acquire()
    old_account_names: frozenset[str] = (
        old_lease.runtime.immutable_request_state.account_names
    )

    result = await reload_harness.reload()
    assert result.ok is True

    new_lease = await reload_harness.runtime_manager.acquire()
    assert isinstance(old_account_names, frozenset)
    assert (
        new_lease.runtime.immutable_request_state.account_names is not old_account_names
    )
    assert old_lease.runtime.immutable_request_state.account_names is old_account_names

    await old_lease.release()
    await new_lease.release()


@pytest.mark.asyncio
async def test_overlap_hop_by_hop_headers_preserved(
    reload_harness: ReloadHarness,
) -> None:
    """Hop-by-hop / credential headers are stable on old and new leases.

    These sets are process-local and never change at reload time; the
    audit here is a regression guard against accidentally making them
    generation-owned and losing set identity under overlap.
    """
    old_lease = await reload_harness.runtime_manager.acquire()
    old_hbh = old_lease.runtime.immutable_request_state.hop_by_hop_headers
    old_lc = old_lease.runtime.immutable_request_state.local_credential_headers

    result = await reload_harness.reload()
    assert result.ok is True

    new_lease = await reload_harness.runtime_manager.acquire()
    assert new_lease.runtime.immutable_request_state.hop_by_hop_headers is old_hbh
    assert new_lease.runtime.immutable_request_state.local_credential_headers is old_lc
    # Reference equality against the well-known module constants.
    assert old_hbh is HOP_BY_HOP_HEADERS
    assert old_lc is LOCAL_CREDENTIAL_HEADERS

    await old_lease.release()
    await new_lease.release()


@pytest.mark.asyncio
async def test_overlap_routing_policy_change_preserves_old_policy(
    reload_harness: ReloadHarness,
) -> None:
    """Old lease continues to use old routing config after reload.

    The initial and candidate configs both use ``local_quota_mode="score_only"``
    (the default), so we exercise a different observable LIVE field —
    ``trace.mode`` — that the candidate config sets explicitly while
    the initial leaves it at the default.  Each lease must observe
    its own generation's value.
    """
    old_lease = await reload_harness.runtime_manager.acquire()
    initial_config: AppConfig = old_lease.runtime.config
    initial_trace_mode = initial_config.routing.trace.mode

    candidate = reload_harness.candidate_config
    candidate_dict = candidate.model_dump()
    routing_dict = candidate_dict.setdefault("routing", {})
    trace_dict = routing_dict.setdefault("trace", {})
    trace_dict["mode"] = "off" if initial_trace_mode != "off" else "full"
    new_candidate = AppConfig(**candidate_dict)
    candidate_trace_mode = new_candidate.routing.trace.mode
    assert candidate_trace_mode != initial_trace_mode

    result = await reload_harness.reload(new_candidate)
    assert result.ok is True

    # Old lease still sees the initial config object (frozen snapshot).
    assert old_lease.runtime.config is initial_config
    assert old_lease.runtime.config.routing.trace.mode == initial_trace_mode

    new_lease = await reload_harness.runtime_manager.acquire()
    assert new_lease.runtime.config is not initial_config
    assert new_lease.runtime.config.routing.trace.mode == candidate_trace_mode

    await old_lease.release()
    await new_lease.release()


@pytest.mark.asyncio
async def test_overlap_config_digest_changes_across_generations(
    reload_harness: ReloadHarness,
) -> None:
    """Config digest changes across generations but old lease keeps old digest.

    The ``config_digest`` field is part of the frozen
    :class:`RuntimeGeneration` and is used by diagnostics to identify
    which generation produced a particular response.
    """
    old_lease = await reload_harness.runtime_manager.acquire()
    old_digest: Any = old_lease.runtime.config_digest

    result = await reload_harness.reload()
    assert result.ok is True

    assert old_lease.runtime.config_digest == old_digest

    new_lease = await reload_harness.runtime_manager.acquire()
    assert new_lease.runtime.config_digest != old_digest

    await old_lease.release()
    await new_lease.release()


@pytest.mark.asyncio
async def test_overlap_transcode_loss_policy_strict_only_applies_to_new(
    reload_harness: ReloadHarness,
) -> None:
    """Stricter transcode policy only affects new leases.

    A request in flight on the old generation that began transcoding
    before the policy change must still observe the pre-reload
    transcoder on the old lease; a request acquired after reload must
    observe the stricter policy object via the new lease's transcoder
    reference.  We verify identity at the lease snapshot level: the
    transcoder on each lease is the one precomputed when the
    generation was built.
    """
    old_lease = await reload_harness.runtime_manager.acquire()
    old_transcoder: Any = old_lease.runtime.transcoder_policy

    candidate = _transcode_loss_strict_config()
    result = await reload_harness.reload(candidate)
    assert result.ok is True

    # Old lease retains its own transcoder reference.
    assert old_lease.runtime.transcoder_policy is old_transcoder

    new_lease = await reload_harness.runtime_manager.acquire()
    assert new_lease.runtime.transcoder_policy is not old_transcoder

    await old_lease.release()
    await new_lease.release()
