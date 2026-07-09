"""Tests for the centralized ``eggpool.jsonx`` JSON backend helper.

The tests are parametrised across both backends so the stdlib fallback
and the ``orjson`` fast path share the same semantic contract.  When
``orjson`` is not installed in the test environment the ``orjson``
parametrisation is skipped (the stdlib suite still runs).
"""

from __future__ import annotations

import importlib
import json
import sys

import pytest

from eggpool import jsonx

_HAS_ORJSON = importlib.util.find_spec("orjson") is not None


_BACKENDS: list[tuple[str, str]] = [
    pytest.param("stdlib", id="stdlib"),
    pytest.param(
        "orjson",
        id="orjson",
        marks=pytest.mark.skipif(not _HAS_ORJSON, reason="orjson not installed"),
    ),
]


def _force_backend(name: str) -> dict[str, object]:
    """Re-import :mod:`eggpool.jsonx` with ``EGGPOOL_JSON_BACKEND`` overridden."""
    saved = jsonx.os.environ.get("EGGPOOL_JSON_BACKEND")
    jsonx.os.environ["EGGPOOL_JSON_BACKEND"] = name
    try:
        reloaded = importlib.reload(jsonx)
    finally:
        if saved is None:
            jsonx.os.environ.pop("EGGPOOL_JSON_BACKEND", None)
        else:
            jsonx.os.environ["EGGPOOL_JSON_BACKEND"] = saved
    return {"reloaded": reloaded}


@pytest.fixture
def _restore_jsonx_backend():
    """Ensure jsonx state survives tests that swap ``EGGPOOL_JSON_BACKEND``."""
    yield
    jsonx.os.environ.pop("EGGPOOL_JSON_BACKEND", None)
    importlib.reload(jsonx)


class TestLoadsInputForms:
    """``loads`` accepts bytes, bytearray, memoryview and str."""

    @pytest.mark.parametrize("backend_name", _BACKENDS)
    def test_loads_bytes(self, backend_name: str, _restore_jsonx_backend: None) -> None:
        reloaded = _force_backend(backend_name)["reloaded"]
        assert reloaded.loads(b'{"a": 1, "b": [1, 2]}') == {"a": 1, "b": [1, 2]}

    @pytest.mark.parametrize("backend_name", _BACKENDS)
    def test_loads_str(self, backend_name: str, _restore_jsonx_backend: None) -> None:
        reloaded = _force_backend(backend_name)["reloaded"]
        assert reloaded.loads('{"a": 1}') == {"a": 1}

    @pytest.mark.parametrize("backend_name", _BACKENDS)
    def test_loads_bytearray(
        self, backend_name: str, _restore_jsonx_backend: None
    ) -> None:
        reloaded = _force_backend(backend_name)["reloaded"]
        assert reloaded.loads(bytearray(b"[1, 2, 3]")) == [1, 2, 3]

    @pytest.mark.parametrize("backend_name", _BACKENDS)
    def test_loads_memoryview(
        self, backend_name: str, _restore_jsonx_backend: None
    ) -> None:
        reloaded = _force_backend(backend_name)["reloaded"]
        assert reloaded.loads(memoryview(b'"hello"')) == "hello"

    @pytest.mark.parametrize("backend_name", _BACKENDS)
    def test_loads_scalar(
        self, backend_name: str, _restore_jsonx_backend: None
    ) -> None:
        reloaded = _force_backend(backend_name)["reloaded"]
        assert reloaded.loads(b"42") == 42
        assert reloaded.loads(b"null") is None
        assert reloaded.loads(b"true") is True

    @pytest.mark.parametrize("backend_name", _BACKENDS)
    def test_loads_invalid_raises(
        self, backend_name: str, _restore_jsonx_backend: None
    ) -> None:
        reloaded = _force_backend(backend_name)["reloaded"]
        with pytest.raises(ValueError):
            reloaded.loads(b"not valid json")

    def test_loads_rejects_other_types(self) -> None:
        with pytest.raises(TypeError):
            jsonx.loads(123)  # type: ignore[arg-type]


class TestDumpsBytesOutput:
    """``dumps_bytes`` returns compact UTF-8 JSON bytes."""

    @pytest.mark.parametrize("backend_name", _BACKENDS)
    def test_compact_no_whitespace(
        self, backend_name: str, _restore_jsonx_backend: None
    ) -> None:
        reloaded = _force_backend(backend_name)["reloaded"]
        out = reloaded.dumps_bytes({"a": 1, "b": [1, 2]})
        assert isinstance(out, bytes)
        assert out == b'{"a":1,"b":[1,2]}'

    @pytest.mark.parametrize("backend_name", _BACKENDS)
    def test_unicode_preserved(
        self, backend_name: str, _restore_jsonx_backend: None
    ) -> None:
        reloaded = _force_backend(backend_name)["reloaded"]
        out = reloaded.dumps_bytes({"name": "café"})
        assert isinstance(out, bytes)
        assert out == '{"name":"café"}'.encode()

    @pytest.mark.parametrize("backend_name", _BACKENDS)
    def test_non_str_keys_handled(
        self, backend_name: str, _restore_jsonx_backend: None
    ) -> None:
        reloaded = _force_backend(backend_name)["reloaded"]
        out = reloaded.dumps_bytes({1: "one"})
        assert isinstance(out, bytes)
        assert json.loads(out) == {"1": "one"}


class TestDumpsStrOutput:
    """``dumps_str`` returns a Python ``str`` suitable for DB / log fields."""

    @pytest.mark.parametrize("backend_name", _BACKENDS)
    def test_returns_str(self, backend_name: str, _restore_jsonx_backend: None) -> None:
        reloaded = _force_backend(backend_name)["reloaded"]
        out = reloaded.dumps_str({"a": 1})
        assert isinstance(out, str)
        assert out == '{"a":1}'


class TestRoundTrip:
    """Round-trip across heterogeneous payload shapes."""

    @pytest.mark.parametrize("backend_name", _BACKENDS)
    @pytest.mark.parametrize(
        "value",
        [
            {"a": 1, "b": [1, 2, 3], "c": None, "d": True, "e": "text"},
            [1, 2, 3, "four", None, {"five": 5}],
            "unicode string with é and 中文",
            42,
            True,
            None,
        ],
        ids=["object", "array", "unicode-str", "int", "bool", "none"],
    )
    def test_round_trip(
        self, backend_name: str, value: object, _restore_jsonx_backend: None
    ) -> None:
        reloaded = _force_backend(backend_name)["reloaded"]
        encoded = reloaded.dumps_bytes(value)
        assert isinstance(encoded, bytes)
        assert json.loads(encoded) == value


class TestBackendSelection:
    """The active backend is reported correctly and respects the override."""

    def test_active_backend_reported(self) -> None:
        assert jsonx.active_backend() in {"orjson", "stdlib"}
        assert jsonx.USING_ORJSON is (jsonx.active_backend() == "orjson")

    def test_force_stdlib(
        self, _restore_jsonx_backend: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EGGPOOL_JSON_BACKEND", "stdlib")
        reloaded = importlib.reload(jsonx)
        assert reloaded.USING_ORJSON is False
        assert reloaded.active_backend() == "stdlib"

    @pytest.mark.skipif(not _HAS_ORJSON, reason="orjson not installed")
    def test_force_orjson(
        self, _restore_jsonx_backend: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EGGPOOL_JSON_BACKEND", "orjson")
        reloaded = importlib.reload(jsonx)
        assert reloaded.USING_ORJSON is True
        assert reloaded.active_backend() == "orjson"

    @pytest.mark.skipif(not _HAS_ORJSON, reason="orjson not installed")
    def test_auto_prefers_orjson(
        self, _restore_jsonx_backend: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("EGGPOOL_JSON_BACKEND", "auto")
        reloaded = importlib.reload(jsonx)
        assert reloaded.USING_ORJSON is True


class TestStreamingFrameCompatibility:
    """Streaming SSE frames emitted by the transcoder remain valid."""

    def test_anthropic_frame_is_valid(self) -> None:
        from eggpool.transcoder.streaming import _BaseStreamingTranscoder

        frame = _BaseStreamingTranscoder._anthropic_frame(
            "message_start", {"type": "message_start"}
        )
        assert frame.startswith(b"event: message_start\n")
        assert frame.endswith(b"\n\n")
        body = frame.split(b"\n", 2)[1]
        assert body.startswith(b"data: ")
        payload = json.loads(body[len(b"data: ") :])
        assert payload == {"type": "message_start"}

    def test_openai_frame_is_valid(self) -> None:
        from eggpool.transcoder.streaming import _BaseStreamingTranscoder

        frame = _BaseStreamingTranscoder._openai_frame(
            {"choices": [{"delta": {"content": "hi"}}]}
        )
        assert frame.startswith(b"data: ")
        assert frame.endswith(b"\n\n")
        payload = json.loads(frame[len(b"data: ") :].rstrip(b"\n"))
        assert payload == {"choices": [{"delta": {"content": "hi"}}]}


class TestEncodeJsonBodyDelegate:
    """``encode_json_body`` continues to produce compact UTF-8 JSON bytes."""

    def test_encode_json_body_compact(self) -> None:
        from eggpool.request.body import encode_json_body

        out = encode_json_body({"a": 1, "b": [1, 2]})
        assert out == b'{"a":1,"b":[1,2]}'

    def test_encode_json_body_unicode(self) -> None:
        from eggpool.request.body import encode_json_body

        out = encode_json_body({"name": "café"})
        assert out == '{"name":"café"}'.encode()


def test_module_public_api() -> None:
    """The module exposes the documented helper API."""
    for name in ("USING_ORJSON", "loads", "dumps_bytes", "dumps_str", "active_backend"):
        assert name in vars(jsonx) or hasattr(jsonx, name), name
    # Confirm ``loads`` is callable from the imported module reference
    assert callable(jsonx.loads)
    assert callable(jsonx.dumps_bytes)
    assert callable(jsonx.dumps_str)
    assert callable(jsonx.active_backend)


def test_module_does_not_import_orjson_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``orjson`` import fails the fallback must still work."""
    # Simulate orjson being unimportable.
    monkeypatch.setitem(sys.modules, "orjson", None)  # type: ignore[arg-type]
    monkeypatch.setenv("EGGPOOL_JSON_BACKEND", "auto")
    reloaded = importlib.reload(jsonx)
    assert reloaded.USING_ORJSON is False
    assert reloaded.dumps_bytes({"a": 1}) == b'{"a":1}'
