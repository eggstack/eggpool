"""Tests for Phase 1: dashboard model-info degraded-state diagnostics.

Covers:
- ``_get_model_info_summary_state`` when service is missing or raises.
- ``render_models`` degraded-warning panel for service_unattached and
  fetch_error.
- ``render_model_detail`` degraded message when lookup fails.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from starlette.requests import Request

from eggpool.dashboard.render import render_model_detail, render_models
from eggpool.dashboard.routes import (
    ModelInfoDashboardState,
    _get_model_info_summary_state,
)
from eggpool.model_info.types import CanonicalModelInfo

pytestmark = pytest.mark.dashboard

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_request(app_state: Any) -> Request:
    """Build a minimal ``starlette.requests.Request`` wrapping *app_state*."""

    class _App:
        state = app_state

    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "path": "/models",
        "headers": [],
        "query_string": b"",
        "app": _App(),
    }

    async def _receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, _receive)


class _StubAppState:
    """Minimal ``app.state`` that intentionally omits ``model_info``."""

    pass


class _StubModelInfoService:
    """Stub that raises on ``get_summary_map``."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def get_summary_map(self, model_ids: Any = None) -> dict[str, Any]:
        raise self._exc


# ---------------------------------------------------------------------------
# Phase 1 — service_unattached
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_state_unattached_when_service_missing() -> None:
    """No ``app.state.model_info`` -> service_unattached degraded reason."""
    state = await _get_model_info_summary_state(
        model_info_service=None,
    )
    assert state.available is False
    assert state.degraded_reason == "service_unattached"
    assert state.summaries == {}


# ---------------------------------------------------------------------------
# Phase 1 — fetch_error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_state_fetch_error_when_summary_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Exception in ``get_summary_map`` -> fetch_error + log + degraded warning."""
    stub = _StubModelInfoService(RuntimeError("boom"))
    with caplog.at_level(logging.WARNING, logger="eggpool.dashboard.routes"):
        state = await _get_model_info_summary_state(stub)
    assert state.available is True
    assert state.degraded_reason == "fetch_error"
    assert state.error_class == "RuntimeError"
    assert state.summaries == {}
    # The logger should have emitted at least one warning-level record.
    assert any(
        "summary" in record.message.lower() or "model_info" in record.message.lower()
        for record in caplog.records
    )


# ---------------------------------------------------------------------------
# Phase 1 — successful fetch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio()
async def test_state_available_when_service_returns_summaries() -> None:
    """Successful ``get_summary_map`` -> available=True, no degraded_reason."""
    now = datetime.now(UTC)

    class _GoodService:
        async def get_summary_map(
            self, model_ids: Any = None
        ) -> dict[str, CanonicalModelInfo]:
            return {
                "gpt-4o": CanonicalModelInfo(
                    model_id="gpt-4o",
                    status="fresh",
                    summary="test summary",
                    sparse=False,
                    detail={},
                    provenance={},
                    conflicts={},
                    first_seen_at=now,
                    last_seen_at=now,
                    last_refreshed_at=now,
                    next_refresh_at=now,
                ),
            }

    state = await _get_model_info_summary_state(_GoodService())
    assert state.available is True
    assert state.degraded_reason is None
    assert state.summary_count == 1
    assert "gpt-4o" in state.summaries


# ---------------------------------------------------------------------------
# Renderer — degraded panels
# ---------------------------------------------------------------------------


def test_render_models_degraded_panel_when_state_unattached() -> None:
    """``render_models`` emits the service-not-attached warning."""
    state = ModelInfoDashboardState(
        summaries={},
        available=False,
        degraded_reason="service_unattached",
        summary_count=0,
    )
    rows = [{"model_id": "gpt-4o", "request_count": 1, "provider_id": "openai"}]
    html = render_models(models=rows, period="24h", model_info_state=state)
    assert "Model info unavailable: service not attached" in html
    assert "gpt-4o" in html


def test_render_models_degraded_panel_when_state_fetch_error() -> None:
    """``render_models`` emits the fetch-error warning."""
    state = ModelInfoDashboardState(
        summaries={},
        available=True,
        degraded_reason="fetch_error",
        error_class="RuntimeError",
        summary_count=0,
    )
    rows = [{"model_id": "gpt-4o", "request_count": 1, "provider_id": "openai"}]
    html = render_models(models=rows, period="24h", model_info_state=state)
    assert "Model info unavailable: summary map fetch failed" in html
    assert "gpt-4o" in html


def test_render_models_no_warning_when_state_available() -> None:
    """No degraded panel when state has no degraded_reason."""
    state = ModelInfoDashboardState(
        summaries={"gpt-4o": {"status": "fresh"}},
        available=True,
        summary_count=1,
    )
    rows = [{"model_id": "gpt-4o", "request_count": 1, "provider_id": "openai"}]
    html = render_models(models=rows, period="24h", model_info_state=state)
    assert "Model info unavailable" not in html


def test_render_model_detail_degraded_when_error() -> None:
    """render_model_detail shows degraded when info=None and error is set."""
    html = render_model_detail(
        info=None, model_id="foo", model_info_error="DatabaseError"
    )
    assert "lookup failed" in html.lower()
    # The model_info_error is not rendered; it only controls the branch.
    assert "foo" in html


def test_render_model_detail_empty_when_no_error() -> None:
    """``render_model_detail`` shows generic unavailable when info=None and no error."""
    html = render_model_detail(info=None, model_id="bar")
    assert "Model info not available" in html
    assert "bar" in html
