"""Tests for the /runtime page rendering after the cache page split."""

from __future__ import annotations

from eggpool.dashboard.render import render_runtime

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


class TestRenderRuntimeCacheLink:
    """render_runtime includes a clear link to the Cache page."""

    def test_cache_link_present(self) -> None:
        html = render_runtime(_MINIMAL_SNAPSHOT)
        assert "/cache?period=" in html

    def test_cache_link_text(self) -> None:
        html = render_runtime(_MINIMAL_SNAPSHOT)
        assert "Cache" in html

    def test_request_shaping_panel_present(self) -> None:
        html = render_runtime(_MINIMAL_SNAPSHOT)
        assert "Request shaping" in html


class TestRenderRuntimePeriodAndTheme:
    """render_runtime passes period through to the Cache link."""

    def test_default_period_in_cache_link(self) -> None:
        html = render_runtime(_MINIMAL_SNAPSHOT, period="24h")
        assert "/cache?period=24h" in html

    def test_custom_period_in_cache_link(self) -> None:
        html = render_runtime(_MINIMAL_SNAPSHOT, period="7d")
        assert "/cache?period=7d" in html
