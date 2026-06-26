"""Tests for OutboundClientManager."""

from __future__ import annotations

import httpx
import pytest

from eggpool.providers.outbound import OutboundClientManager


class TestOutboundClientManager:
    """Tests for OutboundClientManager."""

    @pytest.mark.anyio
    async def test_get_client_returns_same_instance(self) -> None:
        manager = OutboundClientManager()
        client1 = await manager.get_client()
        client2 = await manager.get_client()
        assert client1 is client2
        assert manager.build_count == 1
        await manager.aclose()

    @pytest.mark.anyio
    async def test_build_count_stable(self) -> None:
        manager = OutboundClientManager()
        for _ in range(10):
            await manager.get_client()
        assert manager.build_count == 1
        await manager.aclose()

    @pytest.mark.anyio
    async def test_aclose_idempotent(self) -> None:
        manager = OutboundClientManager()
        await manager.get_client()
        await manager.aclose()
        await manager.aclose()  # Should not raise

    @pytest.mark.anyio
    async def test_aclose_without_get_client(self) -> None:
        manager = OutboundClientManager()
        await manager.aclose()  # Should not raise

    @pytest.mark.anyio
    async def test_build_count_zero_before_first_call(self) -> None:
        manager = OutboundClientManager()
        assert manager.build_count == 0
        await manager.aclose()

    @pytest.mark.anyio
    async def test_client_is_async_client(self) -> None:
        manager = OutboundClientManager()
        client = await manager.get_client()
        assert isinstance(client, httpx.AsyncClient)
        await manager.aclose()

    @pytest.mark.anyio
    async def test_concurrent_get_client(self) -> None:
        import asyncio

        manager = OutboundClientManager()
        results = await asyncio.gather(
            manager.get_client(),
            manager.get_client(),
            manager.get_client(),
        )
        # All should be the same instance
        assert results[0] is results[1] is results[2]
        assert manager.build_count == 1
        await manager.aclose()
