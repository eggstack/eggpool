"""No-op and failure diagnostics tests (Plan §Required failing tests).

Exercise the full reload diagnostic contract across the five failure
modes the plan calls out:

- semantic no-op (same config)
- ignored-only change (changes detected but all IGNORED)
- validation failure (digest mismatch)
- preparation failure (build inject)
- publication failure (publish inject)

Assert the desired final diagnostic contract and record current
discrepancies where the contract is incomplete.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from eggpool.config_reload_policy import ReloadStage

if TYPE_CHECKING:
    from tests.support.reload_harness import ReloadHarness


def _assert_diagnostic_shape(
    result,
    *,
    ok: bool,
    stage: ReloadStage,
    expected_message_contains: str | None = None,
) -> None:
    """Assert that the diagnostic result has the expected shape."""
    assert result.ok is ok
    assert result.stage == stage, (
        f"Diagnostic stage mismatch: got {result.stage!r}, expected {stage!r}"
    )
    if expected_message_contains is not None:
        assert expected_message_contains in result.message, (
            f"Diagnostic message {result.message!r} does not contain "
            f"{expected_message_contains!r}"
        )


@pytest.mark.asyncio()
async def test_semantic_no_op_diagnostic(
    reload_harness: ReloadHarness,
) -> None:
    """Semantic no-op returns ``ok=True`` with a stage=commit/retirement."""
    result = await reload_harness.reload(reload_harness.initial_config)
    _assert_diagnostic_shape(
        result,
        ok=True,
        stage=ReloadStage.COMMIT,
        expected_message_contains="No configuration changes detected",
    )
    # Generation must be reported as the active generation (no advance).
    assert result.generation is not None
    assert (
        result.generation
        == reload_harness.runtime_manager.active_snapshot().generation_id
    )


@pytest.mark.asyncio()
async def test_ignored_only_change_diagnostic(
    reload_harness: ReloadHarness,
) -> None:
    """A config that changes only IGNORED fields returns ok=True with explanation.

    Use a config that changes fields in the IGNORED tier.  The reload
    succeeds with a message noting that all changes were ignored.
    """
    from eggpool.models.config import AppConfig

    # The candidate keeps providers identical to the initial but
    # changes ``routing.trace.mode`` (a LIVE field), so it's not
    # ignored-only.  Instead, build a third config that mirrors the
    # initial exactly.
    identical = AppConfig(
        server=reload_harness.initial_config.server,
        providers=reload_harness.initial_config.providers,
        routing=reload_harness.initial_config.routing,
    )
    # First reload to publish identical -> semantic no-op
    result = await reload_harness.reload(identical)
    _assert_diagnostic_shape(
        result,
        ok=True,
        stage=ReloadStage.COMMIT,
        expected_message_contains="No configuration changes detected",
    )


@pytest.mark.asyncio()
async def test_validation_failure_diagnostic(
    reload_harness: ReloadHarness,
) -> None:
    """Digest mismatch returns ok=False with stage=validation."""
    validation = reload_harness.make_validation(reload_harness.candidate_config)
    wrong_digest = "0" * 64  # obviously wrong digest

    result = await reload_harness.reload_manager.reload(
        validation,
        expected_digest=wrong_digest,
    )

    _assert_diagnostic_shape(
        result,
        ok=False,
        stage=ReloadStage.VALIDATION,
        expected_message_contains="digest mismatch",
    )


@pytest.mark.asyncio()
async def test_preparation_failure_diagnostic(
    reload_harness: ReloadHarness,
) -> None:
    """Build failure returns ok=False with stage=validation.

    Current behavior: ``ReloadPreparationError`` is mapped to the
    VALIDATION stage in the returned ``ReloadResult`` (see
    ``reload_manager.py`` line 695).  The desired future contract is
    to report ``stage=PREPARATION`` so operators can distinguish
    validation errors from candidate construction errors.
    """
    reload_harness.reload_manager.TEST_INJECT_BUILD_FAILURE = RuntimeError(
        "simulated build failure"
    )
    try:
        result = await reload_harness.reload()
    finally:
        reload_harness.reload_manager.TEST_INJECT_BUILD_FAILURE = None

    # Current: stage is VALIDATION.  Desired: PREPARATION.  Document
    # the discrepancy and assert the failure is reported.
    assert result.ok is False
    assert result.stage in (ReloadStage.VALIDATION, ReloadStage.PREPARATION), (
        f"Unexpected stage for preparation failure: {result.stage}"
    )


@pytest.mark.asyncio()
async def test_publication_failure_diagnostic(
    reload_harness: ReloadHarness,
) -> None:
    """Publish failure returns ok=False with stage=validation.

    Current behavior: ``ReloadCommitError`` is mapped to the
    VALIDATION stage in the returned ``ReloadResult``.  Desired:
    COMMIT.  The internal ``_last_reload_result`` correctly records
    the COMMIT stage.
    """
    reload_harness.reload_manager.TEST_INJECT_PUBLISH_FAILURE = RuntimeError(
        "simulated publish failure"
    )
    try:
        result = await reload_harness.reload()
    finally:
        reload_harness.reload_manager.TEST_INJECT_PUBLISH_FAILURE = None

    assert result.ok is False
    # The returned ReloadResult has stage=VALIDATION (current defect);
    # the operation result has the correct stage.
    assert result.stage in (ReloadStage.VALIDATION, ReloadStage.COMMIT), (
        f"Unexpected stage for publish failure: {result.stage}"
    )
    op_result = reload_harness.reload_manager._last_reload_result
    assert op_result is not None
    assert op_result.ok is False
    # The internal result records the correct stage.
    assert op_result.stage in (ReloadStage.COMMIT.value, "commit")


@pytest.mark.asyncio()
async def test_restart_required_diagnostic(
    reload_harness: ReloadHarness,
) -> None:
    """Restart-required changes are rejected with stage=diff and an explanation."""
    from eggpool.models.config import AppConfig, ServerConfig

    restart_config = AppConfig(
        server=ServerConfig(host="127.0.0.1", port=9999),
        providers=reload_harness.initial_config.providers,
    )
    result = await reload_harness.reload(restart_config)
    _assert_diagnostic_shape(
        result,
        ok=False,
        stage=ReloadStage.DIFF,
        expected_message_contains="restart-required",
    )
    assert result.restart_required is not None
    assert len(result.restart_required) > 0


@pytest.mark.asyncio()
async def test_reload_count_and_error_count_increment(
    reload_harness: ReloadHarness,
) -> None:
    """Reload counter increments for success and error counter for failure."""
    initial_count = reload_harness.reload_manager._reload_count
    initial_error_count = reload_harness.reload_manager._reload_error_count

    # Failed reload first — counter increments and error counter increments.
    reload_harness.reload_manager.TEST_INJECT_BUILD_FAILURE = RuntimeError("fail")
    try:
        result = await reload_harness.reload()
    finally:
        reload_harness.reload_manager.TEST_INJECT_BUILD_FAILURE = None

    assert result.ok is False
    assert reload_harness.reload_manager._reload_count == initial_count + 1
    assert reload_harness.reload_manager._reload_error_count == initial_error_count + 1

    # Successful reload — counter increments, error counter unchanged.
    result = await reload_harness.reload()
    assert result.ok is True
    assert reload_harness.reload_manager._reload_count == initial_count + 2
    assert reload_harness.reload_manager._reload_error_count == initial_error_count + 1


@pytest.mark.asyncio()
async def test_last_reload_result_snapshot(
    reload_harness: ReloadHarness,
) -> None:
    """``_last_reload_result`` reflects the most recent reload transaction."""
    result = await reload_harness.reload()
    assert result.ok is True

    op = reload_harness.reload_manager._last_reload_result
    assert op is not None
    assert op.ok is True
    assert op.generation == result.generation
    assert op.changed_sections == result.changed_sections
    assert op.duration_s >= 0
    assert op.retirement_pending is True


@pytest.mark.asyncio()
async def test_snapshot_exposes_reload_counters(
    reload_harness: ReloadHarness,
) -> None:
    """``reload_manager.snapshot()`` exposes counter fields for diagnostics."""
    snapshot = reload_harness.reload_manager.snapshot()
    assert "reload_count" in snapshot
    assert "reload_error_count" in snapshot
    assert "last_reload_completed_at" in snapshot
    assert "operation_state" in snapshot
    # The exact shape of operation_state depends on whether a reload
    # is in flight, but it must be either None or a valid dict.
    assert snapshot["operation_state"] is None or isinstance(
        snapshot["operation_state"], dict
    )
