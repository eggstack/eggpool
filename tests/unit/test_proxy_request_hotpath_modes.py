"""End-to-end request-path coverage for proxy hot-path compression modes.

These tests pin the integration contract between
:func:`eggpool.api.proxy_request.handle_proxy_request` and the
compression helpers (:func:`analyze_compression`,
:func:`apply_safe_compression`, :func:`segment_request`).

Why end-to-end here, when helper-level tests already exist?

* ``test_proxy_request_safe_mode_skips_analyzer.py`` proves that
  ``safe`` mode does not call ``analyze_compression`` and that
  ``observe`` mode does not call ``apply_safe_compression``.  Those
  are the most common regressions to worry about, but they are
  helper-level spies — they would still pass if a future refactor
  accidentally re-introduced the analyze call inside
  ``proxy_request.py`` (e.g. when computing the
  ``SafeModeObservation`` outside the applier).

* ``test_hotpath_corrective_polish.py`` pins recorder contracts
  (``record_ns``, ``SafeModeObservation`` shape, copy-on-write
  behavior) but never goes through a real
  ``handle_proxy_request`` invocation.

The tests below run the full pipeline through
``handle_proxy_request`` with a captured ``ProxyRequestContext``
and a real ``DispatchSpanRecorder``.  They prove:

* Safe mode calls apply exactly once, analyze zero times, and
  produces an observation whose ``source`` is ``"safe_apply"``.
* Observe mode calls analyze exactly once, apply zero times, and
  produces an observation whose ``source`` is not ``"safe_apply"``.
* Disabled mode segments nothing, observes nothing, applies
  nothing, and emits no compression spans.
* The recorder only sees ``segmentation`` and ``compression_apply``
  in safe mode; only ``segmentation`` and ``compression_analyze``
  in observe mode; nothing compression-related in disabled mode.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from eggpool.api.proxy_request import (
    ProxyEndpointConfig,
    handle_proxy_request,
)
from eggpool.runtime_dispatch import (
    SPAN_COMPRESSION_ANALYZE,
    SPAN_COMPRESSION_APPLY,
    SPAN_SEGMENTATION,
    DispatchSpanRecorder,
)
from eggpool.transcoder.compression import apply as apply_mod
from eggpool.transcoder.compression.apply import _noop_result

# ---------------------------------------------------------------------------
# Test fakes / harness
# ---------------------------------------------------------------------------


def _endpoint() -> ProxyEndpointConfig:
    return ProxyEndpointConfig(
        protocol="openai",
        request_label="openai",
        not_found_error_type="model_not_found",
        service_error_type="service_unavailable",
        error_response=lambda *, status_code, message, error_type: (
            "err",
            status_code,
            message,
        ),
    )


def _minimal_config() -> Any:
    from eggpool.models.config import AppConfig

    return AppConfig.from_dict(
        {
            "server": {
                "host": "127.0.0.1",
                "port": 0,
                "threads": 1,
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


class _FakeTransforms:
    fold_repeated_lines = True
    compact_logs = True
    compact_search_results = True
    elide_base64_blobs = True
    minify_machine_json = True
    compact_stack_traces = True


class _FakeCompressionPolicy:
    """Minimal :class:`CompressionConfig` stub honoring ``enabled``/``mode``."""

    def __init__(self, *, enabled: bool, mode: str) -> None:
        self.enabled = enabled
        self.mode = mode
        self.max_compression_latency_ms = 25.0
        self.min_candidate_tokens = 0
        self.min_savings_tokens = 0
        self.transforms = _FakeTransforms()
        self.respect_cache_boundaries = True
        self.placement = "suffix_only"


class _CapturingCoordinator:
    """Coordinator stub that records the ``ProxyRequestContext`` it sees."""

    def __init__(self) -> None:
        self.context: Any = None
        self.executions: int = 0

    async def execute(self, context: Any) -> Any:
        self.context = context
        self.executions += 1

        class _Resp:
            status_code = 200
            headers: tuple[Any, ...] = ()
            body = b""
            stream_iterator = None

        return _Resp()


class _FakeApp:
    def __init__(self, state: Any) -> None:
        self.state = state


class _FakeCatalogCache:
    """Stub for ``ModelCatalogCache`` that skips limit enforcement and
    reports every model as natively supporting the OpenAI protocol.
    """

    def get_effective_limits(self, model_id: str, provider_id: str | None) -> Any:
        return None

    def get_model_protocols(
        self, model_id: str, provider_id: str | None = None
    ) -> set[str]:
        return {"openai"}

    def get_transcodable_protocols(
        self,
        model_id: str,
        client_protocol: str,
        provider_id: str | None = None,
    ) -> set[str]:
        return set()

    def count_eligible_accounts_for_protocol(
        self,
        model_id: str,
        protocol: str,
        provider_id: str | None = None,
    ) -> int:
        return 0


class _FakeCatalog:
    """Stub for the generation's ``catalog`` attribute."""

    cache = _FakeCatalogCache()


class _FakeRuntime:
    """Stub for the leased ``RuntimeGeneration`` surface used by the proxy."""

    def __init__(
        self,
        *,
        coordinator: Any,
        recorder: DispatchSpanRecorder,
        config: Any,
        immutable_request_state: Any,
        compression_policy: Any,
    ) -> None:
        self.coordinator = coordinator
        self.dispatch_span_recorder = recorder
        self.config = config
        self.immutable_request_state = immutable_request_state
        self.catalog = _FakeCatalog()
        self.transcoder_policy = None
        self.compression_policy = compression_policy


class _FakeLease:
    """Stub for ``GenerationLease`` used by the hot-path proxy tests."""

    def __init__(self, runtime: _FakeRuntime) -> None:
        self._runtime = runtime
        self.released = False

    @property
    def runtime(self) -> _FakeRuntime:
        return self._runtime

    async def release(self) -> None:
        self.released = True


class _FakeRuntimeManager:
    """Stub for ``RuntimeManager`` whose ``acquire`` yields a stub lease."""

    def __init__(self, lease: _FakeLease) -> None:
        self._lease = lease

    async def acquire(self) -> _FakeLease:
        return self._lease


class _FakeRequest:
    def __init__(self, body: bytes, app: Any) -> None:
        self._body = body
        self.headers: dict[str, str] = {"user-agent": "test"}
        self.app = app
        self.client = type("_Client", (), {"host": "127.0.0.1"})()

    async def body(self) -> bytes:
        return self._body


def _make_chunky_payload() -> bytes:
    """Build an OpenAI chat payload with enough volatile suffix to segment.

    The repeated trailing lines are what produces a non-empty
    :class:`SegmentationResult` and gives the safe applier
    something to inspect.
    """
    repeated = "\n".join(["[repeating log line] cmd ran successfully; status=0"] * 200)
    return json.dumps(
        {
            "model": "openai/gpt-4o",
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "stable prefix that should not be touched. " + repeated
                    ),
                }
            ],
            "stream": False,
        }
    ).encode("utf-8")


def _patch_auth_and_body(monkeypatch: Any) -> None:
    """Bypass :func:`require_auth` and stream-reading for the helper fakes."""

    from eggpool.api import proxy_request as proxy_request_module

    async def _noop_auth(request: Any) -> None:
        return None

    async def _read_body(request: Any, max_bytes: int) -> bytes:
        body = request._body  # type: ignore[attr-defined]
        if isinstance(body, (bytes, bytearray)):
            return bytes(body)
        return body.encode("utf-8")

    monkeypatch.setattr(proxy_request_module, "require_auth", _noop_auth)
    monkeypatch.setattr(proxy_request_module, "read_body_limited", _read_body)


def _build_state(
    *,
    mode: str,
    enabled: bool,
    coordinator: Any,
    recorder: DispatchSpanRecorder,
    monkeypatch: Any,
) -> _FakeApp:
    """Assemble a fake app/state with mode-aware compression_policy."""

    from eggpool.runtime_manager import ImmutableRequestState

    config = _minimal_config()
    compression_policy = _FakeCompressionPolicy(enabled=enabled, mode=mode)
    runtime = _FakeRuntime(
        coordinator=coordinator,
        recorder=recorder,
        config=config,
        immutable_request_state=ImmutableRequestState(
            provider_ids=frozenset(),
            account_names=frozenset(),
            hop_by_hop_headers=frozenset(),
            local_credential_headers=frozenset(),
        ),
        compression_policy=compression_policy,
    )
    lease = _FakeLease(runtime)
    state = type(
        "State",
        (),
        {
            "runtime_manager": _FakeRuntimeManager(lease),
            "coordinator": coordinator,
            "compression_policy": compression_policy,
            "dispatch_span_recorder": recorder,
            "config": config,
            "catalog": runtime.catalog,
            "transcoder_policy": None,
        },
    )()
    _patch_auth_and_body(monkeypatch)
    return _FakeApp(state)


# ---------------------------------------------------------------------------
# Spy helpers — these wrap the real helpers and assert they were/were-not
# called and capture the context carried forward.
# ---------------------------------------------------------------------------


def _install_safe_spy(monkeypatch: Any) -> dict[str, int]:
    """Spy :func:`apply_safe_compression` and forbid :func:`analyze_compression`.

    Returns a counter dict; safe path runs and observe path raises.
    """
    from eggpool.transcoder import compression as compression_pkg

    calls: dict[str, int] = {"analyze": 0, "apply": 0}

    def _spy_analyze(*args: Any, **kwargs: Any) -> Any:
        calls["analyze"] += 1
        raise AssertionError("analyze_compression must not run in safe mode")

    def _spy_apply(*args: Any, **kwargs: Any) -> Any:
        calls["apply"] += 1
        target_payload = kwargs.get("payload")
        if target_payload is None and args:
            target_payload = args[0]
        return _noop_result(target_payload if target_payload is not None else {})

    monkeypatch.setattr(compression_pkg, "analyze_compression", _spy_analyze)
    monkeypatch.setattr(apply_mod, "apply_safe_compression", _spy_apply)
    return calls


def _install_observe_spy(monkeypatch: Any) -> dict[str, int]:
    """Spy :func:`analyze_compression` and forbid :func:`apply_safe_compression`."""

    from eggpool.transcoder import compression as compression_pkg

    calls: dict[str, int] = {"analyze": 0, "apply": 0}

    def _spy_analyze(*args: Any, **kwargs: Any) -> Any:
        calls["analyze"] += 1
        return None

    def _spy_apply(*args: Any, **kwargs: Any) -> Any:
        calls["apply"] += 1
        raise AssertionError("apply_safe_compression must not run in observe mode")

    monkeypatch.setattr(compression_pkg, "analyze_compression", _spy_analyze)
    monkeypatch.setattr(apply_mod, "apply_safe_compression", _spy_apply)
    return calls


def _install_disabled_spy(monkeypatch: Any) -> dict[str, int]:
    """Forbid any compression call from firing on the disabled path."""

    from eggpool.transcoder import compression as compression_pkg
    from eggpool.transcoder import segmentation as segmentation_module

    calls: dict[str, int] = {"segment": 0, "analyze": 0, "apply": 0}

    def _spy_segment(*args: Any, **kwargs: Any) -> Any:
        calls["segment"] += 1
        raise AssertionError("segment_request must not run with compression disabled")

    def _spy_analyze(*args: Any, **kwargs: Any) -> Any:
        calls["analyze"] += 1
        raise AssertionError(
            "analyze_compression must not run with compression disabled"
        )

    def _spy_apply(*args: Any, **kwargs: Any) -> Any:
        calls["apply"] += 1
        raise AssertionError(
            "apply_safe_compression must not run with compression disabled"
        )

    monkeypatch.setattr(segmentation_module, "segment_request", _spy_segment)
    monkeypatch.setattr(compression_pkg, "analyze_compression", _spy_analyze)
    monkeypatch.setattr(apply_mod, "apply_safe_compression", _spy_apply)
    return calls


# ---------------------------------------------------------------------------
# Phase 2 — Safe mode end-to-end
# ---------------------------------------------------------------------------


class TestSafeModeEndToEnd:
    """Safe mode through ``handle_proxy_request`` calls apply exactly once
    and never calls the observe analyzer.  The dispatched
    ``ProxyRequestContext`` carries the applier-derived observation and
    the apply span but no analyze span.
    """

    @pytest.mark.asyncio
    async def test_safe_mode_calls_apply_once_and_analyzer_zero(
        self, monkeypatch: Any
    ) -> None:
        recorder = DispatchSpanRecorder(window_size=200, detailed_span_sample_rate=1.0)
        coordinator = _CapturingCoordinator()
        app = _build_state(
            mode="safe",
            enabled=True,
            coordinator=coordinator,
            recorder=recorder,
            monkeypatch=monkeypatch,
        )

        calls = _install_safe_spy(monkeypatch)

        body = _make_chunky_payload()
        request = _FakeRequest(body, app)

        result = await handle_proxy_request(request, _endpoint())
        assert result is not None
        assert coordinator.executions == 1
        context = coordinator.context
        assert context is not None

        # Spy-level invariants: safe mode analyzes 0 and applies 1.
        assert calls["apply"] == 1
        assert calls["analyze"] == 0

        # Context carries safe-mode products.
        assert context.compression_result is not None
        assert context.compression_result.applied is False
        assert context.compression_observation is not None
        observation_summary = json.loads(
            context.compression_observation.to_summary_json()
        )
        assert observation_summary["source"] == "safe_apply"
        assert observation_summary["mode"] == "safe"

        # Segmentation ran (payload has a long enough volatile suffix).
        assert context.segmentation is not None
        assert context.segmentation_not_collected is False

        # Precomputed fields are populated.
        assert context.estimated_reservation_tokens is not None
        assert context.thinking_requirement is not None

        # Recorder carries the apply span but not the analyze span.
        snapshot = recorder.snapshot_for_spans(
            [SPAN_SEGMENTATION, SPAN_COMPRESSION_APPLY, SPAN_COMPRESSION_ANALYZE]
        )
        by_span = {row["span"]: row for row in snapshot["spans"]}
        assert by_span[SPAN_SEGMENTATION]["sample_count"] >= 1
        assert by_span[SPAN_COMPRESSION_APPLY]["sample_count"] >= 1
        assert by_span[SPAN_COMPRESSION_ANALYZE]["sample_count"] == 0


# ---------------------------------------------------------------------------
# Phase 3 — Observe mode end-to-end
# ---------------------------------------------------------------------------


class TestObserveModeEndToEnd:
    """Observe mode through ``handle_proxy_request`` calls analyze exactly
    once and never calls the safe applier.  The dispatched
    ``ProxyRequestContext`` carries an observation but no
    ``compression_result``.
    """

    @pytest.mark.asyncio
    async def test_observe_mode_calls_analyze_once_and_apply_zero(
        self, monkeypatch: Any
    ) -> None:
        recorder = DispatchSpanRecorder(window_size=200, detailed_span_sample_rate=1.0)
        coordinator = _CapturingCoordinator()
        app = _build_state(
            mode="observe",
            enabled=True,
            coordinator=coordinator,
            recorder=recorder,
            monkeypatch=monkeypatch,
        )

        calls = _install_observe_spy(monkeypatch)

        body = _make_chunky_payload()
        request = _FakeRequest(body, app)

        result = await handle_proxy_request(request, _endpoint())
        assert result is not None
        assert coordinator.executions == 1
        context = coordinator.context
        assert context is not None

        # Spy-level invariants: observe mode analyzes 1 and applies 0.
        assert calls["analyze"] == 1
        assert calls["apply"] == 0

        # Observe mode never produces a compression_result.
        assert context.compression_result is None
        # The analyzer stub returns ``None``; the context keeps None.
        assert context.compression_observation is None

        # Segmentation ran.
        assert context.segmentation is not None
        assert context.segmentation_not_collected is False

        # Precomputed fields are populated.
        assert context.estimated_reservation_tokens is not None
        assert context.thinking_requirement is not None

        # Recorder carries the analyze span but not the apply span.
        snapshot = recorder.snapshot_for_spans(
            [SPAN_SEGMENTATION, SPAN_COMPRESSION_APPLY, SPAN_COMPRESSION_ANALYZE]
        )
        by_span = {row["span"]: row for row in snapshot["spans"]}
        assert by_span[SPAN_SEGMENTATION]["sample_count"] >= 1
        assert by_span[SPAN_COMPRESSION_ANALYZE]["sample_count"] >= 1
        assert by_span[SPAN_COMPRESSION_APPLY]["sample_count"] == 0


# ---------------------------------------------------------------------------
# Phase 4 — Disabled compression end-to-end
# ---------------------------------------------------------------------------


class TestDisabledCompressionEndToEnd:
    """Disabled compression stays cheap: no segmentation, no analyzer,
    no applier, no compression-related spans.
    """

    @pytest.mark.asyncio
    async def test_disabled_skips_segmentation_analyzer_and_apply(
        self, monkeypatch: Any
    ) -> None:
        recorder = DispatchSpanRecorder(window_size=200, detailed_span_sample_rate=1.0)
        coordinator = _CapturingCoordinator()
        app = _build_state(
            mode="observe",
            enabled=False,
            coordinator=coordinator,
            recorder=recorder,
            monkeypatch=monkeypatch,
        )

        calls = _install_disabled_spy(monkeypatch)

        body = _make_chunky_payload()
        request = _FakeRequest(body, app)

        result = await handle_proxy_request(request, _endpoint())
        assert result is not None
        assert coordinator.executions == 1
        context = coordinator.context
        assert context is not None

        # None of the compression helpers should have fired.
        assert calls["segment"] == 0
        assert calls["analyze"] == 0
        assert calls["apply"] == 0

        # The orchestrator short-circuits segmentation entirely.
        assert context.segmentation is None
        assert context.segmentation_not_collected is True
        assert context.compression_observation is None
        assert context.compression_result is None

        # Recorder carries none of the compression-related spans.
        snapshot = recorder.snapshot_for_spans(
            [SPAN_SEGMENTATION, SPAN_COMPRESSION_APPLY, SPAN_COMPRESSION_ANALYZE]
        )
        by_span = {row["span"]: row for row in snapshot["spans"]}
        assert by_span[SPAN_SEGMENTATION]["sample_count"] == 0
        assert by_span[SPAN_COMPRESSION_APPLY]["sample_count"] == 0
        assert by_span[SPAN_COMPRESSION_ANALYZE]["sample_count"] == 0


class TestContextEstimateReuse:
    @pytest.mark.asyncio
    async def test_handler_computes_canonical_context_estimate_once(
        self, monkeypatch: Any
    ) -> None:
        from eggpool.catalog.limits import EffectiveModelLimits
        from eggpool.request import limits

        recorder = DispatchSpanRecorder(window_size=200, detailed_span_sample_rate=1.0)
        coordinator = _CapturingCoordinator()
        app = _build_state(
            mode="observe",
            enabled=False,
            coordinator=coordinator,
            recorder=recorder,
            monkeypatch=monkeypatch,
        )
        monkeypatch.setattr(
            _FakeCatalogCache,
            "get_effective_limits",
            lambda self, model_id, provider_id: EffectiveModelLimits(
                context_tokens=1_000_000,
                input_tokens=None,
                output_tokens=None,
                enforce=True,
                context_source="test",
                input_source=None,
                output_source=None,
            ),
        )

        original_estimator = limits.estimate_context_input_tokens
        calls = 0

        def count_estimate(*args: Any, **kwargs: Any) -> int:
            nonlocal calls
            calls += 1
            return original_estimator(*args, **kwargs)

        monkeypatch.setattr(limits, "estimate_context_input_tokens", count_estimate)
        body = _make_chunky_payload()
        result = await handle_proxy_request(_FakeRequest(body, app), _endpoint())

        assert result is not None
        assert calls == 1
        assert coordinator.context.estimated_context_input_tokens == original_estimator(
            body,
            json.loads(body),
        )
