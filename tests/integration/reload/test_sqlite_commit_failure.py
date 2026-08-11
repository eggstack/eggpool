"""SQLite commit-failure injection tests.

Verifies that a publish failure (via TEST_INJECT_PUBLISH_FAILURE) rolls
back the entire transaction and leaves active generation ID, config digest,
and all runtime state unchanged.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.support.database_faults import fail_commit
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


# ---------------------------------------------------------------------------
# True SQLite COMMIT failure injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_commit_injection_rolls_back_persistence(
    reload_harness: ReloadHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A COMMIT bypass injection rolls the persistence delta back.

    The reload must fail with ``ok=False``, the active generation ID
    and config digest must remain identical to the pre-reload
    baseline, and the SQLite database must reflect only the
    pre-reload state.
    """
    pre_snapshot = await RuntimeSnapshot.capture_async(
        reload_harness.runtime_manager,
        db=reload_harness.db,
    )

    fail_commit(monkeypatch, reload_harness.db, RuntimeError("simulated commit bypass"))
    result = await reload_harness.reload()

    assert result.ok is False
    # publication_occurred lives on the diagnostic snapshot, not the wire result.
    snapshot = reload_harness.reload_manager.snapshot()
    diag = snapshot.get("last_diagnostic_result") or {}
    assert diag.get("publication_occurred") is False, (
        "Publication must not have occurred before the COMMIT bypass"
    )

    post_snapshot = await RuntimeSnapshot.capture_async(
        reload_harness.runtime_manager,
        db=reload_harness.db,
    )

    gen_diffs = post_snapshot.assert_same_generation(pre_snapshot)
    assert gen_diffs == [], f"Generation changed after commit bypass: {gen_diffs}"
    persist_diffs = post_snapshot.assert_same_persistence(pre_snapshot)
    assert persist_diffs == [], (
        f"Persistence changed after commit bypass: {persist_diffs}"
    )


@pytest.mark.asyncio()
async def test_commit_injection_reopens_lease_admission(
    reload_harness: ReloadHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A COMMIT bypass injection reopens lease admission.

    After the injected failure, the runtime manager must accept new
    leases on the pre-reload active generation — the candidate slot
    must be discarded and the lease gate cleared.
    """
    pre_snapshot = await RuntimeSnapshot.capture_async(
        reload_harness.runtime_manager,
        db=reload_harness.db,
    )

    fail_commit(monkeypatch, reload_harness.db, RuntimeError("simulated commit bypass"))
    await reload_harness.reload()

    lease = await reload_harness.runtime_manager.acquire()
    try:
        post_snapshot = await RuntimeSnapshot.capture_async(
            reload_harness.runtime_manager,
            db=reload_harness.db,
        )
        gen_diffs = post_snapshot.assert_same_generation(pre_snapshot)
        assert gen_diffs == [], (
            f"Active generation changed after lease acquired: {gen_diffs}"
        )
    finally:
        await lease.release()


@pytest.mark.asyncio()
async def test_commit_injection_is_one_shot(
    reload_harness: ReloadHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The COMMIT injection must fire only once.

    Removing the patch after the first failure proves subsequent
    transactions succeed normally.
    """
    fail_commit(
        monkeypatch,
        reload_harness.db,
        RuntimeError("simulated one-shot commit bypass"),
    )
    result = await reload_harness.reload()

    assert result.ok is False

    monkeypatch.undo()
    result2 = await reload_harness.reload()
    assert result2.ok is True, (
        f"Second reload after auto-cleared injection failed: {result2}"
    )
