"""Phase 2 integration-test: proxy handler must NOT call the observe-mode
analyzer when compression mode is ``safe``.

The separate analyzer pass is replaced by a derived observation from
the safe-mode applier to remove a duplicate full compression walk
(see ``plans/python_hotpath_dispatch_compression_optimization.md``).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from eggpool.api.proxy_request import (
    ProxyEndpointConfig,
    handle_proxy_request,
)

pytestmark = pytest.mark.request_path


class _FakeCompressionPolicy:
    """Minimal ``[compression]`` config stub."""

    def __init__(self, mode: str = "safe") -> None:
        self.enabled = True
        self.mode = mode
        self.max_compression_latency_ms = 25.0
        self.min_candidate_tokens = 2048
        self.min_savings_tokens = 1024

        class _Transforms:
            fold_repeated_lines = True
            compact_logs = True
            compact_search_results = True
            elide_base64_blobs = True
            minify_machine_json = True
            compact_stack_traces = True

        self.transforms = _Transforms()
        self.respect_cache_boundaries = True
        self.compress_static_prefix = False
        self.placement = "suffix_only"


class _FakeCoordinator:
    """Minimal coordinator stub that records the post-build context."""

    async def execute(self, context: Any) -> Any:
        class _Resp:
            status_code = 200
            headers: tuple[Any, ...] = ()
            body = b""
            stream_iterator = None

        return _Resp()


def _endpoint() -> ProxyEndpointConfig:
    return ProxyEndpointConfig(
        protocol="openai",
        request_label="openai",
        not_found_error_type="model_not_found",
        service_error_type="service_unavailable",
        error_response=lambda *, status_code, message, error_type:
        ("err", status_code, message),
    )


def _minimal_config() -> Any:
    """Build an :class:`AppConfig` with no providers / cache / force_segmentation.

    The proxy handler reads ``config.providers`` early (for the
    provider-suffix parser) and ``config.cache.synthetic_cache_controls``
    later.  Returning a config that omits both prevents
    ``AttributeError`` without altering analyzer / applier behavior.
    """
    from eggpool.models.config import AppConfig

    return AppConfig.from_dict(
        {
            "server": {
                "host": "127.0.0.1",
                "port": 0,
                "threads": 2,
                "api_key_env": "",
            },
            "database": {"path": ":memory:"},
            "upstream": {"base_url": "http://localhost:19999"},
            "accounts": [],
            "models": {"startup_refresh": False},
            "providers": {},
            "dashboard": {"enabled": False},
        }
    )


async def _invoke_analyzer_skip_for_mode(
    mode: str, monkeypatch: Any
) -> tuple[int, int]:
    """Run ``handle_proxy_request`` once and return analyzer / apply call counts."""
    from eggpool.api import proxy_request as proxy_request_module
    from eggpool.transcoder import compression as compression_pkg
    from eggpool.transcoder.compression import apply as apply_mod

    # Bypass auth to keep the unit test focused on analyzer / applier
    # dispatch.  ``require_auth`` would otherwise require a real API
    # key in scope.
    async def _noop_auth(request: Any) -> None:
        return None

    # Bypass the body-stream reader.  Tests pass the encoded JSON as a
    # single chunk-shaped bytes value so the handler can skip the
    # starlette request stream.
    async def _read_body(request: Any, max_bytes: int) -> bytes:
        body = request._body  # type: ignore[attr-defined]
        if isinstance(body, (bytes, bytearray)):
            return bytes(body)
        return body.encode("utf-8")

    monkeypatch.setattr(proxy_request_module, "require_auth", _noop_auth)
    monkeypatch.setattr(proxy_request_module, "read_body_limited", _read_body)

    calls: dict[str, int] = {"analyze": 0, "apply": 0}

    def _spy_analyze(*args: Any, **kwargs: Any) -> Any:
        calls["analyze"] += 1
        return None

    def _spy_apply(*args: Any, **kwargs: Any) -> Any:
        calls["apply"] += 1
        return apply_mod._noop_result(args[0] if args else {})

    # Patch the public re-exports so ``from eggpool.transcoder.compression
    # import analyze_compression`` resolves to the spy.  The handler
    # import-uses the symbol lazily, so patching either the package or
    # the analyzer module itself is enough.
    monkeypatch.setattr(compression_pkg, "analyze_compression", _spy_analyze)
    monkeypatch.setattr(
        apply_mod, "apply_safe_compression", _spy_apply
    )

    coordinator = _FakeCoordinator()
    state = type(
        "State",
        (),
        {
            "coordinator": coordinator,
            "compression_policy": _FakeCompressionPolicy(mode=mode),
            "compression_tuning_registry": None,
            "dispatch_span_recorder": None,
            "config": _minimal_config(),
            "catalog": None,
            "transcoder_policy": None,
        },
    )()

    class _FakeApp:
        def __init__(self, state: Any) -> None:
            self.state = state

    app = _FakeApp(state)

    class _FakeRequest:
        def __init__(self, body: bytes, app: Any) -> None:
            self._body = body
            self.headers: dict[str, str] = {"user-agent": "test"}
            self.app = app
            self.client = type("_Client", (), {"host": "127.0.0.1"})()

        async def body(self) -> bytes:
            return self._body

    body = json.dumps(
        {
            "model": "openai/gpt-4o",
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "hi with text to make sure observation has a payload"
                    ),
                }
            ],
            "stream": False,
        }
    ).encode("utf-8")
    request = _FakeRequest(body, app)

    result = await handle_proxy_request(request, _endpoint())
    # Confirm the handler reached the coordinator (no error response).
    assert result is not None
    return calls["analyze"], calls["apply"]


class TestProxyHandlerSkipsAnalyzerInSafeMode:
    @pytest.mark.asyncio
    async def test_safe_mode_path_does_not_call_analyze(self, monkeypatch: Any) -> None:
        analyze_calls, apply_calls = await _invoke_analyzer_skip_for_mode(
            "safe", monkeypatch
        )
        assert analyze_calls == 0
        assert apply_calls == 1

    @pytest.mark.asyncio
    async def test_observe_mode_path_does_call_analyze(self, monkeypatch: Any) -> None:
        analyze_calls, apply_calls = await _invoke_analyzer_skip_for_mode(
            "observe", monkeypatch
        )
        assert analyze_calls == 1
        assert apply_calls == 0
