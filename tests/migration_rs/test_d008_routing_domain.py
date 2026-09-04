"""D008 Python oracle assertions for the integrated M5 qualification."""

from __future__ import annotations

import pytest

from tests.migration_rs.test_d001_routing_domain import build_snapshot


@pytest.mark.asyncio
async def test_d008_oracle_composes_all_m5_state_families() -> None:
    snapshot = await build_snapshot()

    assert snapshot.schema_version == "m5-routing-domain-d001/v1"
    assert {row.account_name for row in snapshot.accounts} == {
        "account-a",
        "account-b",
        "disabled-a",
    }
    assert snapshot.routing.eligible_candidates
    assert snapshot.routing.selected_account in snapshot.routing.eligible_candidates
    assert snapshot.routing.claim["durable_persistence"] == "deferred:M7"
    assert snapshot.model_router.cache_outcome == "miss_then_hit"
    assert snapshot.parity["container_representation"] == "semantic"
    assert snapshot.parity["request_persistence"] == "deferred:M7"
    assert snapshot.parity["selector_dispatch"] == "deferred:M7"


@pytest.mark.asyncio
async def test_d008_oracle_remains_secret_and_raw_session_free() -> None:
    snapshot = (await build_snapshot()).dumps()
    lowered = snapshot.lower()
    assert "fixture_api_key" not in lowered
    assert "proxy-password" not in lowered
    assert "authorization: bearer" not in lowered
    assert "fixture raw content" not in lowered
