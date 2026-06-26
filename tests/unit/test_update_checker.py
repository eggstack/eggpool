"""Tests for the periodic PyPI update checker."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from eggpool.update_checker import (
    PYPI_URL,
    UpdateChecker,
    UpdateInfo,
    async_check_for_update,
)


def _fake_response(version: str) -> httpx.Response:
    """Build a real httpx.Response carrying *version* as the PyPI latest."""
    request = httpx.Request("GET", PYPI_URL)
    payload = json.dumps({"info": {"version": version}}).encode("utf-8")
    return httpx.Response(
        status_code=200,
        request=request,
        content=payload,
        headers={"content-type": "application/json"},
    )


def _make_checker(
    *,
    current: str = "0.1.0",
    latest: str = "0.2.0",
    method: str = "pip",
    interval_s: float = 60.0,
    http_get: Any | None = None,
) -> UpdateChecker:
    """Build an UpdateChecker with fully-mocked PyPI + version probes."""
    checker = UpdateChecker(check_interval_s=interval_s)
    checker._version_lookup = lambda _name: current  # type: ignore[assignment]
    checker._install_method_lookup = lambda: method  # type: ignore[assignment]
    checker._http_get = (  # type: ignore[assignment]
        http_get
        if http_get is not None
        else (lambda _url, **_kw: _fake_response(latest))
    )
    return checker


# ---------------------------------------------------------------------------
# UpdateInfo
# ---------------------------------------------------------------------------


class TestUpdateInfo:
    def test_defaults_indicate_no_update(self) -> None:
        info = UpdateInfo()
        assert info.update_available is False
        assert info.to_dict() == {
            "current_version": "",
            "latest_version": "",
            "update_available": False,
            "install_method": "unknown",
            "update_command": "eggpool update",
            "last_check_at": 0.0,
            "last_check_error": "",
        }

    def test_to_dict_round_trips(self) -> None:
        info = UpdateInfo(
            current_version="0.1.0",
            latest_version="0.2.0",
            update_available=True,
            install_method="pipx",
            update_command="eggpool update",
            last_check_at=123.4,
            last_check_error="",
        )
        assert info.to_dict()["current_version"] == "0.1.0"
        assert info.to_dict()["update_available"] is True


# ---------------------------------------------------------------------------
# check_once
# ---------------------------------------------------------------------------


class TestCheckOnce:
    def test_records_update_when_latest_is_newer(self) -> None:
        checker = _make_checker(current="0.1.0", latest="0.2.0")
        info = asyncio.run(checker.check_once())
        assert info.update_available is True
        assert info.current_version == "0.1.0"
        assert info.latest_version == "0.2.0"
        assert info.update_command == "eggpool update"
        assert info.last_check_error == ""

    def test_no_update_when_versions_match(self) -> None:
        checker = _make_checker(current="0.1.0", latest="0.1.0")
        info = asyncio.run(checker.check_once())
        assert info.update_available is False

    def test_no_update_when_current_is_newer(self) -> None:
        checker = _make_checker(current="0.3.0", latest="0.2.0")
        info = asyncio.run(checker.check_once())
        assert info.update_available is False

    def test_records_error_when_pypi_unreachable(self) -> None:
        def boom(_url: str, **_kw: object) -> httpx.Response:
            raise httpx.HTTPError("network down")

        checker = _make_checker(current="0.1.0", http_get=boom)
        info = asyncio.run(checker.check_once())
        assert info.update_available is False
        assert "network down" in info.last_check_error
        # Preserves current_version even when PyPI is unreachable
        assert info.current_version == "0.1.0"

    def test_records_error_when_response_empty(self) -> None:
        request = httpx.Request("GET", PYPI_URL)
        empty = httpx.Response(status_code=200, request=request, content=b"{}")
        checker = _make_checker(current="0.1.0", http_get=lambda _u, **_k: empty)
        info = asyncio.run(checker.check_once())
        assert info.update_available is False
        assert "empty version" in info.last_check_error

    def test_preserves_previous_latest_after_failure(self) -> None:
        """A failed check keeps the previously known latest_version so
        the dashboard indicator still surfaces an update that was found
        on an earlier successful probe."""

        async def two_checks() -> tuple[UpdateInfo, UpdateInfo]:
            first = await checker.check_once()

            # Swap in a failing get after the first successful check.
            def boom(_url: str, **_kw: object) -> httpx.Response:
                raise httpx.HTTPError("offline")

            checker._http_get = boom  # type: ignore[assignment]
            second = await checker.check_once()
            return first, second

        checker = _make_checker(current="0.1.0", latest="0.2.0")
        first, second = asyncio.run(two_checks())
        assert first.update_available is True
        assert second.latest_version == "0.2.0"  # preserved
        assert second.update_available is True  # still surfaced
        assert "offline" in second.last_check_error

    def test_snapshot_returns_immutable_copy(self) -> None:
        checker = _make_checker()
        asyncio.run(checker.check_once())
        snap = checker.snapshot()
        assert isinstance(snap, UpdateInfo)
        assert snap.update_available is True
        # Mutating the snapshot must not bleed into the checker's state.
        object.__setattr__(snap, "update_available", False)
        again = checker.snapshot()
        assert again.update_available is True

    def test_install_method_propagates(self) -> None:
        checker = _make_checker(current="0.1.0", latest="0.2.0", method="uv-tool")
        info = asyncio.run(checker.check_once())
        assert info.install_method == "uv-tool"


# ---------------------------------------------------------------------------
# run_periodic
# ---------------------------------------------------------------------------


class TestRunPeriodic:
    def test_periodic_loop_runs_initial_check_then_sleeps(self) -> None:
        checker = _make_checker(current="0.1.0", latest="0.2.0", interval_s=3600)
        call_count = 0

        async def fake_sleep(_seconds: float) -> None:
            nonlocal call_count
            call_count += 1
            # Cancel after the first sleep so the loop exits cleanly.
            raise asyncio.CancelledError()

        # Monkey-patch asyncio.sleep used inside UpdateChecker.
        original_sleep = asyncio.sleep
        asyncio.sleep = fake_sleep  # type: ignore[assignment]
        try:
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(checker.run_periodic())
        finally:
            asyncio.sleep = original_sleep  # type: ignore[assignment]
        # Initial check ran, then one sleep before cancellation.
        assert call_count == 1
        assert checker.snapshot().update_available is True

    def test_periodic_loop_swallows_check_failures(self) -> None:
        attempts = 0

        def flaky(_url: str, **_kw: object) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.HTTPError("first attempt fails")
            return _fake_response("0.2.0")

        checker = _make_checker(
            current="0.1.0", latest="0.2.0", interval_s=3600, http_get=flaky
        )
        sleep_count = 0

        async def fake_sleep(_seconds: float) -> None:
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count >= 2:
                raise asyncio.CancelledError()

        original_sleep = asyncio.sleep
        asyncio.sleep = fake_sleep  # type: ignore[assignment]
        try:
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(checker.run_periodic())
        finally:
            asyncio.sleep = original_sleep  # type: ignore[assignment]
        # Final snapshot reflects the second (successful) attempt.
        assert checker.snapshot().update_available is True


# ---------------------------------------------------------------------------
# async_check_for_update (CLI helper)
# ---------------------------------------------------------------------------


class TestAsyncCheckForUpdate:
    def test_returns_tuple_on_success(self) -> None:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("httpx.get", lambda *_a, **_k: _fake_response("0.5.0"))
            mp.setattr("importlib.metadata.version", lambda _name: "0.4.0")
            current, latest, error = async_check_for_update()
        assert current == "0.4.0"
        assert latest == "0.5.0"
        assert error == ""

    def test_returns_error_on_failure(self) -> None:
        def boom(*_a: Any, **_k: Any) -> httpx.Response:
            raise httpx.HTTPError("network")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("httpx.get", boom)
            mp.setattr("importlib.metadata.version", lambda _name: "0.4.0")
            current, latest, error = async_check_for_update()
        assert current == "0.4.0"
        assert latest == ""
        assert "network" in error

    def test_returns_error_when_version_missing(self) -> None:
        request = httpx.Request("GET", PYPI_URL)
        empty = httpx.Response(status_code=200, request=request, content=b"{}")
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr("httpx.get", lambda *_a, **_k: empty)
            mp.setattr("importlib.metadata.version", lambda _name: "0.4.0")
            current, latest, error = async_check_for_update()
        assert current == "0.4.0"
        assert latest == ""
        assert "empty version" in error


# ---------------------------------------------------------------------------
# schedule_check
# ---------------------------------------------------------------------------


def test_schedule_check_returns_callable_that_produces_coroutine() -> None:
    from eggpool.update_checker import schedule_check

    checker = _make_checker()
    factory = schedule_check(checker)
    assert callable(factory)
    coro = factory()
    # Bound-method coroutines are awaitable; close to avoid warnings.
    coro.close()


# ---------------------------------------------------------------------------
# Shared client integration
# ---------------------------------------------------------------------------


class TestUpdateCheckerSharedClient:
    """Tests for UpdateChecker with a shared httpx.AsyncClient."""

    @pytest.mark.anyio
    async def test_check_once_uses_shared_client(self) -> None:
        """When _client is set, check_once uses it instead of httpx.get()."""
        call_log: list[str] = []

        async def mock_get(url: str, **kwargs: object) -> httpx.Response:
            call_log.append(f"async_get:{url}")
            return _fake_response("0.2.0")

        checker = UpdateChecker(check_interval_s=60.0)
        checker._version_lookup = lambda _name: "0.1.0"  # type: ignore[assignment]
        checker._install_method_lookup = lambda: "pip"  # type: ignore[assignment]
        # Create a mock async client
        mock_client = httpx.AsyncClient()
        mock_client.get = mock_get  # type: ignore[assignment]
        checker._client = mock_client

        info = await checker.check_once()
        assert info.update_available is True
        assert info.latest_version == "0.2.0"
        assert len(call_log) == 1
        assert call_log[0].startswith("async_get:")
        await mock_client.aclose()

    @pytest.mark.anyio
    async def test_check_once_falls_back_to_sync_without_client(self) -> None:
        """Without _client, check_once falls back to httpx.get()."""
        checker = UpdateChecker(check_interval_s=60.0)
        checker._version_lookup = lambda _name: "0.1.0"  # type: ignore[assignment]
        checker._install_method_lookup = lambda: "pip"  # type: ignore[assignment]
        # _client is None by default
        assert checker._client is None
        # This should use the sync path (httpx.get)
        info = await checker.check_once()
        # Will fail with network error in test env, but shouldn't crash
        assert info.current_version == "0.1.0"


class TestAsyncCheckForUpdateSharedClient:
    """Tests for async_check_for_update (sync CLI helper)."""

    def test_sync_function_signature(self) -> None:
        """async_check_for_update is a sync function for CLI paths."""
        import inspect

        from eggpool.update_checker import async_check_for_update

        assert not inspect.iscoroutinefunction(async_check_for_update)
