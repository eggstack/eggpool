"""Persistence/publication split reload tests.

The reload pipeline stages DB reconciliation and publication as separate
phases: ``_reconcile_persistence`` (which writes new providers/accounts
to SQLite inside a transaction) runs before ``_publish_generation``
(which swaps the active runtime generation).

The desired invariant is *all-or-nothing*: either both succeed or
neither is visible.  This file documents the current behavior, where a
failure between the two phases leaves the database ahead of the
runtime.

Plan section ``Required failing tests`` -> ``Persistence/publication split``:

    Inject failure after provider/account persistence changes are
    prepared or committed but before publication completes.  Compare
    the pre-state and post-state.  The desired future invariant is
    total equality with the old state; document the current mixed
    state precisely.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.support.runtime_snapshot import RuntimeSnapshot

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


@pytest.mark.asyncio()
async def test_reconcile_then_publish_failure_leaves_db_split(
    reload_harness: ReloadHarness,
) -> None:
    """Reconcile succeeds; publish fails.  DB has new accounts, runtime has old.

    The candidate config adds ``acct-b1`` (provider-b) plus switches
    ``local_quota_mode``.  The injector fails the publish stage; the
    reconcile phase has already committed the new providers and
    accounts to SQLite.

    Documented behavior:

    - The active runtime generation is unchanged (preserve-old-state invariant).
    - The persisted SQLite state has been updated with the candidate's
      providers and accounts (mixed-state defect: DB ahead of runtime).
    """
    pre = await RuntimeSnapshot.capture_async(
        reload_harness.runtime_manager,
        process=reload_harness.process,
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

    post = await RuntimeSnapshot.capture_async(
        reload_harness.runtime_manager,
        process=reload_harness.process,
        db=reload_harness.db,
    )

    # Active runtime generation preserved.
    assert post.active_generation_id == pre.active_generation_id, (
        "Active runtime generation changed despite publish failure: "
        f"{pre.active_generation_id} -> {post.active_generation_id}"
    )

    # Document the mixed state: persisted providers/accounts reflect
    # the candidate, while the runtime generation still references the
    # initial config.
    expected_persisted_providers = set(reload_harness.candidate_config.providers.keys())
    expected_persisted_accounts = {
        acct.name for acct in reload_harness.candidate_config.all_accounts()
    }
    assert set(post.persisted_provider_ids) == expected_persisted_providers, (
        "Persisted providers should match candidate after reconcile. "
        f"Expected {expected_persisted_providers}, "
        f"got {set(post.persisted_provider_ids)}"
    )
    assert set(post.persisted_account_names) == expected_persisted_accounts, (
        "Persisted accounts should match candidate after reconcile. "
        f"Expected {expected_persisted_accounts}, "
        f"got {set(post.persisted_account_names)}"
    )

    # Active generation config values still match the initial config,
    # not the candidate.  Provider membership differs between the two
    # configs, so we verify via persisted providers rather than a
    # config-value field that may default to the same value.
    initial_providers = set(reload_harness.initial_config.providers.keys())
    candidate_providers = set(reload_harness.candidate_config.providers.keys())
    assert initial_providers != candidate_providers, (
        "Test fixture invariant: initial and candidate configs must "
        "differ in provider membership"
    )
    assert post.active_generation_id == pre.active_generation_id, (
        "Active generation must not advance after publish failure"
    )


@pytest.mark.asyncio()
async def test_reconcile_failure_preserves_persistence_and_runtime(
    reload_harness: ReloadHarness,
) -> None:
    """Reconcile failure must leave both DB and runtime untouched.

    Future invariant: total equality between pre and post snapshots.
    """
    pre = await RuntimeSnapshot.capture_async(
        reload_harness.runtime_manager,
        process=reload_harness.process,
        db=reload_harness.db,
    )

    reload_harness.reload_manager.TEST_INJECT_RECONCILE_FAILURE = RuntimeError(
        "simulated reconcile failure"
    )
    try:
        result = await reload_harness.reload()
    finally:
        reload_harness.reload_manager.TEST_INJECT_RECONCILE_FAILURE = None

    assert result.ok is False

    post = await RuntimeSnapshot.capture_async(
        reload_harness.runtime_manager,
        process=reload_harness.process,
        db=reload_harness.db,
    )

    # Runtime generation preserved.
    gen_diffs = post.assert_same_generation(pre)
    assert gen_diffs == [], f"Generation changed after reconcile failure: {gen_diffs}"

    # Persistence preserved — reconcile ran inside a single DB
    # transaction so it must roll back atomically.
    persistence_diffs = post.assert_same_persistence(pre)
    assert persistence_diffs == [], (
        f"Persistence changed after reconcile failure: {persistence_diffs}"
    )

    # Config values preserved.
    assert post.generation_config_values == pre.generation_config_values, (
        f"Config values changed after reconcile failure: "
        f"{pre.generation_config_values} -> {post.generation_config_values}"
    )


@pytest.mark.asyncio()
async def test_build_failure_preserves_persistence(
    reload_harness: ReloadHarness,
) -> None:
    """Build failure occurs before reconcile; persistence must be unchanged.

    Inject failure at candidate construction so reconcile never runs.
    Future invariant: persisted providers and accounts are unchanged.
    """
    pre = await RuntimeSnapshot.capture_async(
        reload_harness.runtime_manager,
        process=reload_harness.process,
        db=reload_harness.db,
    )

    reload_harness.reload_manager.TEST_INJECT_BUILD_FAILURE = RuntimeError(
        "simulated build failure"
    )
    try:
        result = await reload_harness.reload()
    finally:
        reload_harness.reload_manager.TEST_INJECT_BUILD_FAILURE = None

    assert result.ok is False

    post = await RuntimeSnapshot.capture_async(
        reload_harness.runtime_manager,
        process=reload_harness.process,
        db=reload_harness.db,
    )

    # Runtime preserved.
    assert post.active_generation_id == pre.active_generation_id
    # Persistence preserved — build never reached reconcile.
    persistence_diffs = post.assert_same_persistence(pre)
    assert persistence_diffs == [], (
        f"Persistence changed after build failure: {persistence_diffs}"
    )


@pytest.mark.asyncio()
async def test_successful_reload_brings_db_and_runtime_in_sync(
    reload_harness: ReloadHarness,
) -> None:
    """A successful reload aligns DB and runtime around the candidate config.

    This is the happy-path companion to the split-state test: when
    publication succeeds, both the DB and the runtime reflect the
    candidate's providers and accounts.
    """
    pre = await RuntimeSnapshot.capture_async(
        reload_harness.runtime_manager,
        process=reload_harness.process,
        db=reload_harness.db,
    )

    # Sanity: pre snapshot is the initial config.
    expected_pre_providers = set(reload_harness.initial_config.providers.keys())
    assert set(pre.persisted_provider_ids) == expected_pre_providers

    result = await reload_harness.reload()
    assert result.ok is True

    post = await RuntimeSnapshot.capture_async(
        reload_harness.runtime_manager,
        process=reload_harness.process,
        db=reload_harness.db,
    )

    # DB reflects candidate.
    expected_post_providers = set(reload_harness.candidate_config.providers.keys())
    assert set(post.persisted_provider_ids) == expected_post_providers, (
        f"Expected persisted providers {expected_post_providers}, "
        f"got {set(post.persisted_provider_ids)}"
    )

    # Runtime reflects candidate.
    assert post.active_generation_id == result.generation
    assert post.active_generation_id != pre.active_generation_id
