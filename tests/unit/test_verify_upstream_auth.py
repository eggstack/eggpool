"""Unit tests for ``scripts/verify_upstream_auth.py``.

The verifier is a thin deployment-only script that bypasses the
proxy and calls the upstream OpenAI-compatible and Anthropic-
compatible endpoints directly. These tests use a mocked HTTP
transport to verify:

1. Both endpoint families receive correct auth headers.
2. The key value never appears in stdout or stderr.
3. The response body never appears in stdout or stderr.
4. A failed authentication produces a nonzero exit status.
5. Transport errors produce a concise non-secret diagnostic.
"""

from __future__ import annotations

import sys
from typing import Any

import httpx
import pytest
import verify_upstream_auth  # noqa: E402  (path setup in tests/conftest.py)

UPSTREAM_KEY = "sk-test-verifier-1234567890abcdef"


class _TransportState:
    """Capture sent requests and stage canned responses per family."""

    def __init__(
        self,
        *,
        openai_status: int = 200,
        anthropic_status: int = 200,
        openai_body: bytes = b'{"id":"x","choices":[]}',
        anthropic_body: bytes = b'{"id":"x","content":[]}',
        openai_request_id: str = "openai-req-1",
        anthropic_request_id: str = "anthropic-req-1",
    ) -> None:
        self.openai_status = openai_status
        self.anthropic_status = anthropic_status
        self.openai_body = openai_body
        self.anthropic_body = anthropic_body
        self.openai_request_id = openai_request_id
        self.anthropic_request_id = anthropic_request_id
        self.captured: dict[str, dict[str, Any]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/chat/completions"):
            self.captured["openai"] = {
                "headers": dict(request.headers),
                "body": request.content,
            }
            return httpx.Response(
                self.openai_status,
                headers={"x-request-id": self.openai_request_id},
                content=self.openai_body,
            )
        if path.endswith("/messages"):
            self.captured["anthropic"] = {
                "headers": dict(request.headers),
                "body": request.content,
            }
            return httpx.Response(
                self.anthropic_status,
                headers={"anthropic-request-id": self.anthropic_request_id},
                content=self.anthropic_body,
            )
        if path.endswith("/models") or path.endswith("/models/list"):
            self.captured["models"] = {
                "headers": dict(request.headers),
                "body": request.content,
            }
            return httpx.Response(
                200,
                content=b'{"data":[{"id":"m","object":"model"}]}',
            )
        return httpx.Response(599, content=b"unrouted")

    def get(self, family: str) -> dict[str, Any]:
        return self.captured.get(family, {})


def _install_mock_client(
    monkeypatch: pytest.MonkeyPatch,
    state: _TransportState,
) -> None:
    """Replace the verifier's ``_make_client`` with one that uses the
    supplied transport state.
    """

    def _factory() -> httpx.Client:
        return httpx.Client(
            transport=httpx.MockTransport(state.handler),
            timeout=5.0,
        )

    monkeypatch.setattr(verify_upstream_auth, "_make_client", _factory)


def _capture_transport(
    **kwargs: Any,
) -> _TransportState:
    return _TransportState(**kwargs)


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOROUTER_UPSTREAM_BASE_URL", "https://upstream.example.com")
    monkeypatch.setenv("GOROUTER_TEST_UPSTREAM_KEY", UPSTREAM_KEY)
    monkeypatch.setenv("GOROUTER_OPENAI_MODEL", "gpt-4")
    monkeypatch.setenv("GOROUTER_ANTHROPIC_MODEL", "claude-3-5-sonnet")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "GOROUTER_UPSTREAM_BASE_URL",
        "GOROUTER_TEST_UPSTREAM_KEY",
        "GOROUTER_OPENAI_MODEL",
        "GOROUTER_ANTHROPIC_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)


def _run_main_legacy(monkeypatch: pytest.MonkeyPatch) -> int:
    """Run main() in legacy mode (no --config flag) by patching sys.argv."""
    monkeypatch.setattr(sys, "argv", ["verify_upstream_auth"])
    return verify_upstream_auth.main()


class TestHeaderValidation:
    """Both endpoint families receive correct auth headers."""

    def test_openai_receives_bearer_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_required_env(monkeypatch)
        state = _capture_transport()
        _install_mock_client(monkeypatch, state)
        rc = _run_main_legacy(monkeypatch)
        assert rc == 0
        captured = state.get("openai")
        assert "authorization" in captured["headers"]
        assert captured["headers"]["authorization"] == f"Bearer {UPSTREAM_KEY}"
        assert "x-api-key" not in {k.lower() for k in captured["headers"]}

    def test_anthropic_receives_bearer_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_required_env(monkeypatch)
        state = _capture_transport()
        _install_mock_client(monkeypatch, state)
        rc = _run_main_legacy(monkeypatch)
        assert rc == 0
        captured = state.get("anthropic")
        assert "authorization" in captured["headers"]
        assert captured["headers"]["authorization"] == f"Bearer {UPSTREAM_KEY}"
        assert "x-api-key" not in {k.lower() for k in captured["headers"]}

    def test_no_x_api_key_is_sent_for_either_family(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_required_env(monkeypatch)
        state = _capture_transport()
        _install_mock_client(monkeypatch, state)
        _run_main_legacy(monkeypatch)
        for family in ("openai", "anthropic"):
            captured = state.get(family)
            assert "x-api-key" not in {k.lower() for k in captured["headers"]}, (
                f"x-api-key present in {family} headers"
            )


class TestSecretPrivacy:
    """Key value, prompts, and response bodies never appear in
    stdout or stderr produced by ``main``.
    """

    def test_key_value_absent_from_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _set_required_env(monkeypatch)
        state = _capture_transport()
        _install_mock_client(monkeypatch, state)
        rc = _run_main_legacy(monkeypatch)
        assert rc == 0
        captured = capsys.readouterr()
        assert UPSTREAM_KEY not in captured.out
        assert UPSTREAM_KEY not in captured.err
        assert "Bearer " + UPSTREAM_KEY not in captured.out
        assert "Bearer " + UPSTREAM_KEY not in captured.err

    def test_response_content_absent_from_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _set_required_env(monkeypatch)
        secret_body = b'{"id":"x","choices":[{"message":{"content":"private text"}}]}'
        secret_anthropic = b'{"id":"x","content":[{"text":"private anthropic text"}]}'
        state = _capture_transport(
            openai_body=secret_body, anthropic_body=secret_anthropic
        )
        _install_mock_client(monkeypatch, state)
        rc = _run_main_legacy(monkeypatch)
        assert rc == 0
        captured = capsys.readouterr()
        for secret in (b"private text", b"private anthropic text"):
            assert secret.decode() not in captured.out
            assert secret.decode() not in captured.err


class TestFailureExitStatus:
    """One failed endpoint produces a nonzero exit status."""

    def test_openai_401_produces_nonzero_exit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _set_required_env(monkeypatch)
        state = _capture_transport(openai_status=401, anthropic_status=200)
        _install_mock_client(monkeypatch, state)
        rc = _run_main_legacy(monkeypatch)
        assert rc != 0
        captured = capsys.readouterr()
        assert "FAIL" in captured.out
        assert "openai" in captured.out

    def test_anthropic_403_produces_nonzero_exit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _set_required_env(monkeypatch)
        state = _capture_transport(openai_status=200, anthropic_status=403)
        _install_mock_client(monkeypatch, state)
        rc = _run_main_legacy(monkeypatch)
        assert rc != 0
        captured = capsys.readouterr()
        assert "FAIL" in captured.out
        assert "anthropic" in captured.out

    def test_both_families_failing_produces_nonzero_exit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _set_required_env(monkeypatch)
        state = _capture_transport(openai_status=500, anthropic_status=502)
        _install_mock_client(monkeypatch, state)
        rc = _run_main_legacy(monkeypatch)
        assert rc != 0


class TestTransportErrors:
    """Transport errors produce a concise non-secret diagnostic."""

    def test_transport_error_does_not_echo_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _set_required_env(monkeypatch)

        def _explode(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("upstream unreachable")

        transport = httpx.MockTransport(_explode)

        def _factory() -> httpx.Client:
            return httpx.Client(transport=transport, timeout=5.0)

        monkeypatch.setattr(verify_upstream_auth, "_make_client", _factory)
        rc = _run_main_legacy(monkeypatch)
        assert rc != 0
        captured = capsys.readouterr()
        assert UPSTREAM_KEY not in captured.out
        assert UPSTREAM_KEY not in captured.err
        assert "Bearer " not in captured.out
        assert "Bearer " not in captured.err


class TestEnvironment:
    """The script fails fast when required env vars are missing."""

    def test_missing_base_url_exits_2(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("GOROUTER_TEST_UPSTREAM_KEY", UPSTREAM_KEY)
        monkeypatch.setenv("GOROUTER_OPENAI_MODEL", "gpt-4")
        monkeypatch.setenv("GOROUTER_ANTHROPIC_MODEL", "claude-3-5-sonnet")
        monkeypatch.setattr(sys, "argv", ["verify_upstream_auth"])
        with pytest.raises(SystemExit) as exc:
            verify_upstream_auth.main()
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "GOROUTER_UPSTREAM_BASE_URL" in err

    def test_missing_upstream_key_exits_2(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("GOROUTER_UPSTREAM_BASE_URL", "https://upstream.example.com")
        monkeypatch.setenv("GOROUTER_OPENAI_MODEL", "gpt-4")
        monkeypatch.setenv("GOROUTER_ANTHROPIC_MODEL", "claude-3-5-sonnet")
        monkeypatch.setattr(sys, "argv", ["verify_upstream_auth"])
        with pytest.raises(SystemExit) as exc:
            verify_upstream_auth.main()
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "GOROUTER_TEST_UPSTREAM_KEY" in err

    def test_missing_openai_model_uses_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("GOROUTER_UPSTREAM_BASE_URL", "https://upstream.example.com")
        monkeypatch.setenv("GOROUTER_TEST_UPSTREAM_KEY", UPSTREAM_KEY)
        monkeypatch.delenv("GOROUTER_OPENAI_MODEL", raising=False)
        monkeypatch.delenv("GOROUTER_ANTHROPIC_MODEL", raising=False)
        state = _capture_transport()
        _install_mock_client(monkeypatch, state)
        monkeypatch.setattr(sys, "argv", ["verify_upstream_auth"])
        # Should not raise — defaults to "gpt-4" / "claude-3-5-sonnet"
        rc = verify_upstream_auth.main()
        assert rc == 0

    def test_missing_anthropic_model_uses_default(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("GOROUTER_UPSTREAM_BASE_URL", "https://upstream.example.com")
        monkeypatch.setenv("GOROUTER_TEST_UPSTREAM_KEY", UPSTREAM_KEY)
        monkeypatch.delenv("GOROUTER_OPENAI_MODEL", raising=False)
        monkeypatch.delenv("GOROUTER_ANTHROPIC_MODEL", raising=False)
        state = _capture_transport()
        _install_mock_client(monkeypatch, state)
        monkeypatch.setattr(sys, "argv", ["verify_upstream_auth"])
        # Should not raise — defaults to "gpt-4" / "claude-3-5-sonnet"
        rc = verify_upstream_auth.main()
        assert rc == 0


class TestSecretAbsenceInAllPaths:
    """Even on failure paths, the key must not leak to stdout or stderr."""

    def test_failure_path_does_not_leak_key(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _set_required_env(monkeypatch)
        state = _capture_transport(openai_status=401, anthropic_status=403)
        _install_mock_client(monkeypatch, state)
        rc = _run_main_legacy(monkeypatch)
        assert rc != 0
        captured = capsys.readouterr()
        assert UPSTREAM_KEY not in captured.out
        assert UPSTREAM_KEY not in captured.err
        assert "Bearer " + UPSTREAM_KEY not in captured.out
        assert "Bearer " + UPSTREAM_KEY not in captured.err
