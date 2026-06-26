"""Tests for OutboundClientManager."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from eggpool.models.config import NetworkConfig
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

    @pytest.mark.anyio
    async def test_inject_client_replaces_internal(self) -> None:
        manager = OutboundClientManager()
        mock_client = httpx.AsyncClient()
        manager.inject_client(mock_client)
        client = await manager.get_client()
        assert client is mock_client
        # build_count stays 0 because we skipped _build_client
        assert manager.build_count == 0
        await mock_client.aclose()

    @pytest.mark.anyio
    async def test_snapshot_before_and_after(self) -> None:
        manager = OutboundClientManager()
        snap = manager.snapshot()
        assert snap["build_count"] == 0
        assert snap["request_count"] == 0
        assert snap["error_count"] == 0
        assert snap["has_client"] is False
        assert snap["scopes"] == {"global": 0}
        assert snap["per_host_requests"] == {}
        assert snap["per_host_errors"] == {}

        await manager.get_client()
        snap = manager.snapshot()
        assert snap["build_count"] == 1
        assert snap["has_client"] is True
        assert snap["scopes"] == {"global": 1}
        await manager.aclose()

    @pytest.mark.anyio
    async def test_record_request_counts(self) -> None:
        manager = OutboundClientManager()
        manager.record_request(success=True)
        manager.record_request(success=True)
        manager.record_request(success=False)
        assert manager.request_count == 3
        assert manager.error_count == 1
        await manager.aclose()

    @pytest.mark.anyio
    async def test_record_request_per_host(self) -> None:
        manager = OutboundClientManager()
        manager.record_request(success=True, host="api.example.com")
        manager.record_request(success=True, host="api.example.com")
        manager.record_request(success=False, host="api.other.com")
        snap = manager.snapshot()
        assert snap["per_host_requests"] == {
            "api.example.com": 2,
            "api.other.com": 1,
        }
        assert snap["per_host_errors"] == {"api.other.com": 1}
        await manager.aclose()


class TestOutboundClientManagerConfig:
    """Tests for OutboundClientManager with NetworkConfig."""

    @pytest.mark.anyio
    async def test_config_applied_to_client(self) -> None:
        config = NetworkConfig(
            connect_timeout_s=5.0,
            read_timeout_s=60.0,
            max_connections=20,
            max_keepalive=8,
            keepalive_expiry_s=120.0,
        )
        manager = OutboundClientManager(config=config)
        client = await manager.get_client()
        # Verify the timeout was applied
        assert client.timeout.connect == 5.0
        assert client.timeout.read == 60.0
        assert manager.build_count == 1
        await manager.aclose()

    @pytest.mark.anyio
    async def test_config_none_uses_defaults(self) -> None:
        manager = OutboundClientManager(config=None)
        client = await manager.get_client()
        # Default connect timeout is 10.0
        assert client.timeout.connect == 10.0
        assert client.timeout.read == 30.0
        await manager.aclose()

    def test_network_config_validation(self) -> None:
        """NetworkConfig rejects max_keepalive > max_connections."""
        with pytest.raises(Exception, match="max_keepalive"):
            NetworkConfig(max_connections=4, max_keepalive=8)


class TestOutboundClientManagerStreaming:
    """Tests verifying shared client does not buffer streaming responses."""

    @pytest.mark.anyio
    async def test_streaming_response_not_buffered(self) -> None:
        """The shared client must support incremental body reading."""
        import respx

        manager = OutboundClientManager()
        client = await manager.get_client()

        chunks = [b"chunk1\n", b"chunk2\n", b"chunk3\n"]

        async def _aiter_chunks():  # type: ignore[no-untyped-def]
            for chunk in chunks:
                yield chunk

        with respx.mock:
            route = respx.get("https://example.com/stream").mock(
                return_value=httpx.Response(
                    200,
                    stream=_aiter_chunks(),
                )
            )
            collected: list[bytes] = []
            async with client.stream("GET", "https://example.com/stream") as response:
                async for chunk in response.aiter_bytes():
                    collected.append(chunk)

            assert collected == chunks
            assert route.called

        await manager.aclose()

    @pytest.mark.anyio
    async def test_streaming_text_not_buffered(self) -> None:
        """The shared client supports incremental text reading."""
        import respx

        manager = OutboundClientManager()
        client = await manager.get_client()

        async def _aiter_text():  # type: ignore[no-untyped-def]
            yield b"hello "
            yield b"world"

        with respx.mock:
            respx.get("https://example.com/text").mock(
                return_value=httpx.Response(
                    200,
                    stream=_aiter_text(),
                )
            )
            text_parts: list[str] = []
            async with client.stream("GET", "https://example.com/text") as response:
                async for line in response.aiter_lines():
                    text_parts.append(line)

            assert "".join(text_parts) == "hello world"

        await manager.aclose()

    @pytest.mark.anyio
    async def test_non_streaming_response_still_works(self) -> None:
        """Non-streaming requests through the shared client work normally."""
        import respx

        manager = OutboundClientManager()
        client = await manager.get_client()

        with respx.mock:
            respx.get("https://example.com/data").mock(
                return_value=httpx.Response(200, json={"status": "ok"})
            )
            response = await client.get("https://example.com/data")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}

        await manager.aclose()
