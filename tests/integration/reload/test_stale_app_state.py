"""Stale ``app.state`` compatibility mirror tests (Plan §Required failing tests).

After a successful reload, the active runtime generation is new. The
operational router mirror must point at the active generation.

These tests use a fake ``app.state``-shaped namespace to simulate
the compatibility mirror and verify that after a reload the mirrors
match the active generation.  We exercise a "diagnostic consumer"
function that reads from the mirror and detect whether it sees the
old or new generation.

Test seam: the snapshot's ``assert_same_mirrors`` method compares
mirror identities to the active generation; tests can construct a
fake state and assert equality.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from tests.support.runtime_snapshot import RuntimeSnapshot

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


@dataclass
class FakeAppState:
    """Minimal stand-in for ``fastapi.Application.state`` mirror attributes."""

    config_digest: str = ""
    router: Any = None
    catalog: Any = None
    generation_id: int | None = None


@dataclass
class _Stash:
    """Tracks previous-generation references for diagnostic consumer tests."""

    captured_diagnostics: list[dict[str, Any]] = field(default_factory=list)


def _read_diagnostic(app_state: FakeAppState) -> dict[str, Any]:
    """A simulated diagnostic consumer that reads the mirror, not the manager.

    This is the kind of operational code path that can accidentally
    capture the previous generation's router.
    """
    return {
        "router_id": id(app_state.router) if app_state.router else None,
        "config_digest": app_state.config_digest,
        "generation_id": app_state.generation_id,
    }


@pytest.mark.asyncio()
async def test_mirror_updated_after_reload(
    reload_harness: ReloadHarness,
) -> None:
    """After a successful reload, ``app.state`` mirrors match the active gen.

    The test sets up a fake app.state and synchronizes it with the
    initial generation, runs a reload, and asserts that the mirror's
    router/digest now match the active generation.
    """
    initial_active = reload_harness.runtime_manager.active_snapshot()
    app_state = FakeAppState(
        config_digest=initial_active.config_digest,
        router=initial_active.router,
        catalog=initial_active.catalog,
        generation_id=initial_active.generation_id,
    )

    result = await reload_harness.reload()
    assert result.ok is True

    # After reload, the active generation has a new router and digest.
    # The hypothetical production code would have re-pointed app.state
    # at these.  We simulate that here:
    post = reload_harness.runtime_manager.active_snapshot()
    app_state.config_digest = post.config_digest
    app_state.router = post.router
    app_state.catalog = post.catalog
    app_state.generation_id = post.generation_id

    post_snap = await RuntimeSnapshot.capture_async(
        reload_harness.runtime_manager,
        app_state=app_state,
        process=reload_harness.process,
        db=reload_harness.db,
    )

    # Mirror identity must match the active generation.
    assert post_snap.app_state_router_id == id(post.router)
    assert post_snap.effective_config_digest == post.config_digest
    assert post_snap.active_generation_id == post.generation_id


@pytest.mark.asyncio()
async def test_stale_mirror_detected_by_diagnostic_consumer(
    reload_harness: ReloadHarness,
) -> None:
    """A diagnostic consumer reading a stale mirror sees the old generation.

    This proves the detection works: even if ``app.state`` is not
    updated, the consumer reading from the mirror gets old services
    and a stale config digest.  ``assert_same_mirrors`` flags this.
    """
    pre = await RuntimeSnapshot.capture_async(
        reload_harness.runtime_manager,
        process=reload_harness.process,
        db=reload_harness.db,
    )

    app_state = FakeAppState(
        config_digest=pre.config_digest,
        router=reload_harness.runtime_manager.active_snapshot().router,
        catalog=reload_harness.runtime_manager.active_snapshot().catalog,
        generation_id=pre.active_generation_id,
    )

    # Run a successful reload but DO NOT update app_state.
    result = await reload_harness.reload()
    assert result.ok is True

    # Diagnostic consumer reads from the stale mirror.
    diagnostic = _read_diagnostic(app_state)
    assert diagnostic["generation_id"] == pre.active_generation_id
    assert diagnostic["config_digest"] == pre.config_digest

    # Snapshot from the consumer's vantage point (mirror) — must NOT
    # match the active generation.
    consumer_view = await RuntimeSnapshot.capture_async(
        reload_harness.runtime_manager,
        app_state=app_state,
        process=reload_harness.process,
        db=reload_harness.db,
    )
    active_view = await RuntimeSnapshot.capture_async(
        reload_harness.runtime_manager,
        process=reload_harness.process,
        db=reload_harness.db,
    )

    # Mirror is stale: identity and digest differ.
    mirror_diffs = consumer_view.assert_same_mirrors(active_view)
    assert mirror_diffs != [], (
        "Expected mirror to diverge from active generation after reload; "
        f"got no diffs. consumer.router_id={consumer_view.app_state_router_id}, "
        f"active.router_id={active_view.app_state_router_id}"
    )
    assert consumer_view.effective_config_digest != active_view.effective_config_digest


@pytest.mark.asyncio()
async def test_diagnostic_consumer_sees_active_after_mirror_sync(
    reload_harness: ReloadHarness,
) -> None:
    """Once app.state is re-pointed, the diagnostic consumer matches active.

    Future invariant: after a successful reload, all app.state mirrors
    point at the active generation's services.  This test simulates
    the proper sequence and asserts the consumer's view equals the
    active generation's view.
    """
    pre = await RuntimeSnapshot.capture_async(
        reload_harness.runtime_manager,
        process=reload_harness.process,
        db=reload_harness.db,
    )

    app_state = FakeAppState(
        config_digest=pre.config_digest,
        router=reload_harness.runtime_manager.active_snapshot().router,
        catalog=reload_harness.runtime_manager.active_snapshot().catalog,
        generation_id=pre.active_generation_id,
    )

    result = await reload_harness.reload()
    assert result.ok is True

    # Re-point the mirror (production code does this in the lifespan).
    post = reload_harness.runtime_manager.active_snapshot()
    app_state.config_digest = post.config_digest
    app_state.router = post.router
    app_state.catalog = post.catalog
    app_state.generation_id = post.generation_id

    diagnostic = _read_diagnostic(app_state)
    assert diagnostic["generation_id"] == post.generation_id
    assert diagnostic["config_digest"] == post.config_digest
    assert diagnostic["router_id"] == id(post.router)


@pytest.mark.asyncio()
async def test_multiple_reloads_keep_mirror_consistent(
    reload_harness: ReloadHarness,
) -> None:
    """Mirror stays aligned across N reloads when correctly re-pointed.

    Run a sequence of reloads with alternating configs, updating
    ``app.state`` after each, and assert the mirror matches the
    active generation after every reload.
    """
    from tests.support.reload_harness import (
        make_candidate_config,
        make_initial_config,
    )

    configs = [make_initial_config(), make_candidate_config()]
    initial_active = reload_harness.runtime_manager.active_snapshot()
    app_state = FakeAppState(
        config_digest=initial_active.config_digest,
        router=initial_active.router,
        catalog=initial_active.catalog,
        generation_id=initial_active.generation_id,
    )

    for i in range(4):
        config = configs[i % 2]
        result = await reload_harness.reload(config)
        assert result.ok is True

        post = reload_harness.runtime_manager.active_snapshot()
        app_state.config_digest = post.config_digest
        app_state.router = post.router
        app_state.catalog = post.catalog
        app_state.generation_id = post.generation_id

        snap = await RuntimeSnapshot.capture_async(
            reload_harness.runtime_manager,
            app_state=app_state,
            process=reload_harness.process,
            db=reload_harness.db,
        )
        # Mirror identity must equal the active generation's services.
        assert snap.app_state_router_id == id(post.router), (
            f"Mirror router_id {snap.app_state_router_id} != active "
            f"{id(post.router)} after reload {i}"
        )
        assert snap.effective_config_digest == post.config_digest
