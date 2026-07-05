"""Tests for the /runtime page rendering after the cache page split."""

from __future__ import annotations

import pytest

from eggpool.dashboard.render import render_runtime

pytestmark = pytest.mark.dashboard

_MINIMAL_SNAPSHOT: dict = {
    "server": {},
    "memory": {},
    "processes": {},
    "db": {},
    "routing_runtime": {},
    "outbound_client": {},
    "provider_client_pool": {},
    "dns_cache": {},
    "load": {},
    "dispatch_overhead": {},
}


class TestRenderRuntimeAbsentCachePanels:
    """render_runtime no longer renders the old advanced cache/compression panels."""

    def test_no_advanced_request_shaping_details(self) -> None:
        html = render_runtime(_MINIMAL_SNAPSHOT)
        assert "advanced-request-shaping" not in html
        assert "<details" not in html

    def test_no_cache_reporting_heading(self) -> None:
        html = render_runtime(_MINIMAL_SNAPSHOT)
        assert "Cache reporting (" not in html

    def test_no_request_segmentation_heading(self) -> None:
        html = render_runtime(_MINIMAL_SNAPSHOT)
        assert "Request segmentation (" not in html

    def test_no_compression_opportunities_heading(self) -> None:
        html = render_runtime(_MINIMAL_SNAPSHOT)
        assert "Compression opportunities (" not in html

    def test_no_compression_runtime_heading(self) -> None:
        html = render_runtime(_MINIMAL_SNAPSHOT)
        assert "Compression runtime (" not in html

    def test_no_compression_policies_heading(self) -> None:
        html = render_runtime(_MINIMAL_SNAPSHOT)
        assert "Compression policies (" not in html

    def test_no_synthetic_cache_controls_heading(self) -> None:
        html = render_runtime(_MINIMAL_SNAPSHOT)
        assert "Synthetic cache controls (" not in html

    def test_no_advisory_tuning_heading(self) -> None:
        html = render_runtime(_MINIMAL_SNAPSHOT)
        assert "Advisory tuning (" not in html

    def test_no_cache_diagnostics_link_panel(self) -> None:
        html = render_runtime(_MINIMAL_SNAPSHOT)
        assert 'id="cache-diagnostics-link"' not in html
        assert "Cache &amp; request shaping" not in html

    def test_no_routing_guardrails_panel(self) -> None:
        html = render_runtime(_MINIMAL_SNAPSHOT)
        assert "Routing guardrails" not in html
