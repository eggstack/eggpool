"""Tests for ``eggpool.cli_exit_codes``.

The closure-pass plan (§6.2) defines stable exit codes that scripts
and deployment tooling can rely on.  These tests pin the mapping
between :class:`ControlResponse` failure stages and exit codes.
"""

from __future__ import annotations

from eggpool.cli_exit_codes import (
    EXIT_CONTROL_UNAVAILABLE,
    EXIT_DIGEST_MISMATCH,
    EXIT_OK,
    EXIT_PREPARATION_FAILED,
    EXIT_RESTART_REQUIRED,
    EXIT_VALIDATION,
    exit_code_for_failure,
)


class TestExitCodeMapping:
    def test_restart_required_returns_2(self) -> None:
        """Any non-ok response with restart-required fields is exit 2."""
        code = exit_code_for_failure(
            stage="diff",
            restart_required=("server.port",),
            message="server.port: 8000 -> 9000 requires restart",
        )
        assert code == EXIT_RESTART_REQUIRED

    def test_digest_mismatch_returns_6(self) -> None:
        code = exit_code_for_failure(
            stage="diff",
            restart_required=(),
            message="digest_mismatch: CLI saw abc, server saw def",
        )
        assert code == EXIT_DIGEST_MISMATCH

    def test_validation_stage_returns_1(self) -> None:
        code = exit_code_for_failure(
            stage="validation",
            restart_required=(),
            message="missing required credential",
        )
        assert code == EXIT_VALIDATION

    def test_preparation_stage_returns_5(self) -> None:
        code = exit_code_for_failure(
            stage="preparation",
            restart_required=(),
            message="candidate construction failed",
        )
        assert code == EXIT_PREPARATION_FAILED

    def test_reconciliation_stage_returns_5(self) -> None:
        code = exit_code_for_failure(
            stage="reconciliation",
            restart_required=(),
            message="persistence failed",
        )
        assert code == EXIT_PREPARATION_FAILED

    def test_commit_stage_returns_5(self) -> None:
        code = exit_code_for_failure(
            stage="commit",
            restart_required=(),
            message="publication guard tripped",
        )
        assert code == EXIT_PREPARATION_FAILED

    def test_unknown_stage_returns_validation_default(self) -> None:
        """Unknown stages fall back to validation (1) for fail-closed safety."""
        code = exit_code_for_failure(
            stage="unknown",
            restart_required=(),
            message="something",
        )
        assert code == EXIT_VALIDATION

    def test_restart_required_wins_over_stage(self) -> None:
        """Restart-required overrides the stage mapping."""
        code = exit_code_for_failure(
            stage="preparation",
            restart_required=("server.port",),
            message="",
        )
        assert code == EXIT_RESTART_REQUIRED


class TestExitCodeConstants:
    """Pin the actual numeric values so scripts and docs stay aligned."""

    def test_ok_is_zero(self) -> None:
        assert EXIT_OK == 0

    def test_validation_is_one(self) -> None:
        assert EXIT_VALIDATION == 1

    def test_restart_required_is_two(self) -> None:
        assert EXIT_RESTART_REQUIRED == 2

    def test_control_unavailable_is_three(self) -> None:
        assert EXIT_CONTROL_UNAVAILABLE == 3

    def test_preparation_failed_is_five(self) -> None:
        assert EXIT_PREPARATION_FAILED == 5

    def test_digest_mismatch_is_six(self) -> None:
        assert EXIT_DIGEST_MISMATCH == 6
