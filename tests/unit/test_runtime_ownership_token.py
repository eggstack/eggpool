"""Runtime ownership token and finalization identity tests."""

from __future__ import annotations

import asyncio

import pytest

from eggpool.request.finalization_job import (
    AttemptRuntimeLease,
    FinalizationIdentity,
)

# ---------------------------------------------------------------------------
# FinalizationIdentity
# ---------------------------------------------------------------------------


class TestFinalizationIdentity:
    """FinalizationIdentity is frozen and carries all required fields."""

    def _make_identity(self, **overrides: object) -> FinalizationIdentity:
        defaults = dict(
            proxy_request_id="req-1",
            db_request_id="db-req-1",
            attempt_id=1,
            reservation_id="res-1",
            account_id=10,
            account_name="acct",
            provider_id="openai",
            model_id="gpt-4",
            client_protocol="openai",
            upstream_protocol="openai",
            attempt_number=1,
        )
        defaults.update(overrides)
        return FinalizationIdentity(**defaults)  # type: ignore[arg-type]

    def test_frozen(self) -> None:
        identity = self._make_identity()
        with pytest.raises(AttributeError):
            identity.proxy_request_id = "changed"  # type: ignore[misc]

    def test_all_fields_accessible(self) -> None:
        identity = self._make_identity()
        assert identity.proxy_request_id == "req-1"
        assert identity.db_request_id == "db-req-1"
        assert identity.attempt_id == 1
        assert identity.reservation_id == "res-1"
        assert identity.account_id == 10
        assert identity.account_name == "acct"
        assert identity.provider_id == "openai"
        assert identity.model_id == "gpt-4"
        assert identity.client_protocol == "openai"
        assert identity.upstream_protocol == "openai"
        assert identity.attempt_number == 1

    def test_slots(self) -> None:
        identity = self._make_identity()
        with pytest.raises(AttributeError):
            identity.proxy_request_id = "changed"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# AttemptRuntimeLease
# ---------------------------------------------------------------------------


class TestAttemptRuntimeLease:
    """AttemptRuntimeLease provides idempotent release semantics."""

    def test_release_once_is_idempotent(self) -> None:
        lease = AttemptRuntimeLease(
            account_name="acct",
            active_count_acquired=True,
        )
        assert not lease.released
        # First release
        asyncio.run(lease.release_once(reason="test"))
        assert lease.released
        # Second release is a no-op
        outcomes_2 = asyncio.run(lease.release_once(reason="test"))
        assert outcomes_2 == []

    def test_release_with_router(self) -> None:
        """Active count release calls router.release_active."""
        released_accounts: list[str] = []

        class FakeRouter:
            def release_active(self, account_name: str) -> None:
                released_accounts.append(account_name)

        lease = AttemptRuntimeLease(
            account_name="acct",
            active_count_acquired=True,
        )
        outcomes = asyncio.run(lease.release_once(reason="test", router=FakeRouter()))
        assert len(outcomes) == 1
        assert outcomes[0].component == "active_count"
        assert outcomes[0].released is True
        assert released_accounts == ["acct"]

    def test_release_with_quota_estimator(self) -> None:
        """Quota reservation release calls estimator.release_reservation."""
        released: list[tuple[str, int]] = []

        class FakeEstimator:
            def release_reservation(self, account_name: str, tokens: int) -> None:
                released.append((account_name, tokens))

        lease = AttemptRuntimeLease(
            account_name="acct",
            estimated_tokens=100,
            quota_reservation_acquired=True,
        )
        outcomes = asyncio.run(
            lease.release_once(reason="test", quota_estimator=FakeEstimator())
        )
        assert len(outcomes) == 1
        assert outcomes[0].component == "quota_reservation"
        assert released == [("acct", 100)]

    def test_release_with_health_manager(self) -> None:
        """Health probe release calls health_manager.release_request."""
        released: list[str] = []

        class FakeHealth:
            def release_request(self, account_name: str) -> None:
                released.append(account_name)

        lease = AttemptRuntimeLease(
            account_name="acct",
            health_probe_acquired=True,
        )
        outcomes = asyncio.run(
            lease.release_once(reason="test", health_manager=FakeHealth())
        )
        assert len(outcomes) == 1
        assert outcomes[0].component == "health_probe"
        assert released == ["acct"]

    def test_release_no_acquired_components(self) -> None:
        """No components acquired → no release calls."""
        lease = AttemptRuntimeLease(account_name="acct")
        outcomes = asyncio.run(lease.release_once(reason="test"))
        assert outcomes == []

    def test_release_component_error_captured(self) -> None:
        """Component release errors are captured, not raised."""

        class FailingRouter:
            def release_active(self, account_name: str) -> None:
                raise RuntimeError("release failed")

        lease = AttemptRuntimeLease(
            account_name="acct",
            active_count_acquired=True,
        )
        outcomes = asyncio.run(
            lease.release_once(reason="test", router=FailingRouter())
        )
        assert len(outcomes) == 1
        assert outcomes[0].released is False
        assert "RuntimeError" in (outcomes[0].error or "")

    def test_release_all_components(self) -> None:
        """All three components released in one call."""
        lease = AttemptRuntimeLease(
            account_name="acct",
            estimated_tokens=50,
            active_count_acquired=True,
            quota_reservation_acquired=True,
            health_probe_acquired=True,
        )

        class FakeRouter:
            def release_active(self, account_name: str) -> None:
                pass

        class FakeEstimator:
            def release_reservation(self, account_name: str, tokens: int) -> None:
                pass

        class FakeHealth:
            def release_request(self, account_name: str) -> None:
                pass

        outcomes = asyncio.run(
            lease.release_once(
                reason="test",
                router=FakeRouter(),
                quota_estimator=FakeEstimator(),
                health_manager=FakeHealth(),
            )
        )
        assert len(outcomes) == 3
        assert all(o.released for o in outcomes)
        components = {o.component for o in outcomes}
        assert components == {"active_count", "quota_reservation", "health_probe"}
