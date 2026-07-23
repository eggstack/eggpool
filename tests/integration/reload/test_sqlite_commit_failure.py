"""SQLite commit-failure injection tests (Plan 015 Milestone D1).

Verifies that a publish failure (via TEST_INJECT_PUBLISH_FAILURE) rolls
back the entire transaction and leaves active generation ID, config digest,
and all runtime state unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.support.runtime_snapshot import RuntimeSnapshot

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


@pytest.mark.asyncio()
async def test_publish_failure_preserves_generation_and_runtime_state(
    reload_harness: ReloadHarness,
) -> None:
    """A publish failure rolls back the entire transaction.

    The active generation ID, config digest, service identities, lease
    counts, and persistence state must all remain identical to the
    pre-reload baseline.
    """
    pre_snapshot = await RuntimeSnapshot.capture_async(
        reload_harness.runtime_manager,
        db=reload_harness.db,
    )

    reload_harness.reload_manager.TEST_INJECT_PUBLISH_FAILURE = RuntimeError(
        "simulated publish failure"
    )
    try:
        result = await reload_harness.reload()
    finally:
        reload_harness.reload_manager.TEST_INJECT_PUBLISH_FAILURE = None

    assert result.ok is False

    post_snapshot = await RuntimeSnapshot.capture_async(
        reload_harness.runtime_manager,
        db=reload_harness.db,
    )

    gen_diffs = post_snapshot.assert_same_generation(pre_snapshot)
    assert gen_diffs == [], f"Generation changed after publish failure: {gen_diffs}"

    svc_diffs = post_snapshot.assert_same_services(pre_snapshot)
    assert svc_diffs == [], f"Services changed after publish failure: {svc_diffs}"

    mirror_diffs = post_snapshot.assert_same_mirrors(pre_snapshot)
    assert mirror_diffs == [], f"Mirrors changed after publish failure: {mirror_diffs}"

    persist_diffs = post_snapshot.assert_same_persistence(pre_snapshot)
    assert persist_diffs == [], (
        f"Persistence changed after publish failure: {persist_diffs}"
    )


@pytest.mark.asyncio()
async def test_publish_failure_does_not_advance_generation_id(
    reload_harness: ReloadHarness,
) -> None:
    """After a publish failure the generation ID must not increment."""
    pre_gen_id = reload_harness.runtime_manager.active_snapshot().generation_id

    reload_harness.reload_manager.TEST_INJECT_PUBLISH_FAILURE = RuntimeError(
        "simulated publish failure"
    )
    try:
        result = await reload_harness.reload()
    finally:
        reload_harness.reload_manager.TEST_INJECT_PUBLISH_FAILURE = None

    assert result.ok is False

    post_gen_id = reload_harness.runtime_manager.active_snapshot().generation_id
    assert post_gen_id == pre_gen_id, (
        f"Generation ID advanced from {pre_gen_id} to {post_gen_id} "
        "after publish failure"
    )


@pytest.mark.asyncio()
async def test_publish_failure_does_not_leak_resources(
    reload_harness: ReloadHarness,
) -> None:
    """A publish failure must not leak candidate resources."""
    pre_snapshot = await RuntimeSnapshot.capture_async(
        reload_harness.runtime_manager,
        db=reload_harness.db,
    )

    reload_harness.reload_manager.TEST_INJECT_PUBLISH_FAILURE = RuntimeError(
        "simulated publish failure"
    )
    try:
        result = await reload_harness.reload()
    finally:
        reload_harness.reload_manager.TEST_INJECT_PUBLISH_FAILURE = None

    assert result.ok is False

    post_snapshot = await RuntimeSnapshot.capture_async(
        reload_harness.runtime_manager,
        db=reload_harness.db,
    )

    leak_diffs = post_snapshot.assert_no_resource_leak(pre_snapshot)
    assert leak_diffs == [], f"Resource leak after publish failure: {leak_diffs}"
