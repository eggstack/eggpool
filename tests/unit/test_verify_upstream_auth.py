"""Unit tests for ``scripts/verify_upstream_auth.py``.

The verifier is a thin deployment-only script that bypasses the
proxy and calls the upstream OpenAI-compatible and Anthropic-
compatible endpoints directly with ``Authorization: Bearer``.
These tests use a mocked HTTP transport to verify:

1. Both endpoint families receive exactly one ``Authorization``
   header.
2. No ``x-api-key`` header is sent.
3. The key value never appears in stdout or stderr.
4. The response body never appears in stdout or stderr.
5. A failed authentication produces a nonzero exit status.
6. Transport errors produce a concise non-secret diagnostic.
"""

from __future__ import annotations

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
        openai_status: int,
        anthropic_status: int,
        openai_body: bytes,
        anthropic_body: bytes,
        openai_request_id: str,
        anthropic_request_id: str,
    ) -> None:
        self.openai_status = openai_status
        self.anthropic_status = anthropic_status
        self.openai_body = openai_body
        self.anthropic_body = anthropic_body
        self.openai_request_id = openai_request_id
        self.anthropic_request_id = anthropic_request_id
        self.captured: dict[str, dict[str, Any]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == verify_upstream_auth.OPENAI_PATH:
            self.captured["openai"] = {
                "headers": dict(request.headers),
                "body": request.content,
            }
            return httpx.Response(
                self.openai_status,
                headers={"x-request-id": self.openai_request_id},
                content=self.openai_body,
            )
        if request.url.path == verify_upstream_auth.ANTHROPIC_PATH:
            self.captured["anthropic"] = {
                "headers": dict(request.headers),
                "body": request.content,
            }
            return httpx.Response(
                self.anthropic_status,
                headers={"anthropic-request-id": self.anthropic_request_id},
                content=self.anthropic_body,
            )
        return httpx.Response(599, content=b"unrouted")

    def get(self, family: str) -> dict[str, Any]:
        return self.captured.get(family, {})


def _install_mock_client(
    monkeypatch: pytest.MonkeyPatch,
    state: _TransportState,
) -> None:
    """Replace the verifier's ``_make_client`` with one that uses the
    supplied transport state. The replacement never enables HTTPX
    debug logging.
    """

    def _factory() -> httpx.Client:
        return httpx.Client(
            transport=httpx.MockTransport(state.handler),
            timeout=5.0,
        )

    monkeypatch.setattr(verify_upstream_auth, "_make_client", _factory)


def _capture_transport(
    *,
    openai_status: int = 200,
    anthropic_status: int = 200,
    openai_body: bytes = b'{"id":"x","choices":[]}',
    anthropic_body: bytes = b'{"id":"x","content":[]}',
    openai_request_id: str = "openai-req-1",
    anthropic_request_id: str = "anthropic-req-1",
) -> _TransportState:
    return _TransportState(
        openai_status=openai_status,
        anthropic_status=anthropic_status,
        openai_body=openai_body,
        anthropic_body=anthropic_body,
        openai_request_id=openai_request_id,
        anthropic_request_id=anthropic_request_id,
    )


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


class TestHeaderValidation:
    """Both endpoint families receive exactly one ``Authorization``
    header and no ``x-api-key`` header.
    """

    def test_openai_receives_exactly_one_bearer_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_required_env(monkeypatch)
        state = _capture_transport()
        client = httpx.Client(transport=httpx.MockTransport(state.handler), timeout=5.0)
        try:
            request, headers = verify_upstream_auth._build_openai_request(
                "https://upstream.example.com", UPSTREAM_KEY, "gpt-4"
            )
            expected = f"Bearer {UPSTREAM_KEY}"
            assert verify_upstream_auth._has_exactly_one_bearer(request, expected)
            assert headers["authorization"] == expected
            result = verify_upstream_auth._run_check(
                client, "openai", request, expected
            )
        finally:
            client.close()
        assert result.ok
        captured = state.get("openai")
        auth_headers = [
            v for k, v in captured["headers"].items() if k.lower() == "authorization"
        ]
        assert len(auth_headers) == 1
        assert auth_headers[0] == f"Bearer {UPSTREAM_KEY}"
        assert "x-api-key" not in {k.lower() for k in captured["headers"]}

    def test_anthropic_receives_exactly_one_bearer_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_required_env(monkeypatch)
        state = _capture_transport()
        client = httpx.Client(transport=httpx.MockTransport(state.handler), timeout=5.0)
        try:
            request, headers = verify_upstream_auth._build_anthropic_request(
                "https://upstream.example.com",
                UPSTREAM_KEY,
                "claude-3-5-sonnet",
            )
            expected = f"Bearer {UPSTREAM_KEY}"
            assert verify_upstream_auth._has_exactly_one_bearer(request, expected)
            assert headers["authorization"] == expected
            result = verify_upstream_auth._run_check(
                client, "anthropic", request, expected
            )
        finally:
            client.close()
        assert result.ok
        captured = state.get("anthropic")
        auth_headers = [
            v for k, v in captured["headers"].items() if k.lower() == "authorization"
        ]
        assert len(auth_headers) == 1
        assert auth_headers[0] == f"Bearer {UPSTREAM_KEY}"
        assert "x-api-key" not in {k.lower() for k in captured["headers"]}

    def test_no_x_api_key_is_sent_for_either_family(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_required_env(monkeypatch)
        state = _capture_transport()
        client = httpx.Client(transport=httpx.MockTransport(state.handler), timeout=5.0)
        try:
            request_openai, _ = verify_upstream_auth._build_openai_request(
                "https://upstream.example.com", UPSTREAM_KEY, "gpt-4"
            )
            request_anthropic, _ = verify_upstream_auth._build_anthropic_request(
                "https://upstream.example.com",
                UPSTREAM_KEY,
                "claude-3-5-sonnet",
            )
            expected = f"Bearer {UPSTREAM_KEY}"
            verify_upstream_auth._run_check(client, "openai", request_openai, expected)
            verify_upstream_auth._run_check(
                client, "anthropic", request_anthropic, expected
            )
        finally:
            client.close()
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
        state = _capture_transport(
            openai_body=b'{"id":"x","choices":[]}',
            anthropic_body=b'{"id":"x","content":[]}',
        )
        _install_mock_client(monkeypatch, state)
        rc = verify_upstream_auth.main()
        assert rc == 0
        captured = capsys.readouterr()
        assert UPSTREAM_KEY not in captured.out
        assert UPSTREAM_KEY not in captured.err
        # Bearer prefix alone is also not allowed to leak.
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
        rc = verify_upstream_auth.main()
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
        rc = verify_upstream_auth.main()
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
        rc = verify_upstream_auth.main()
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
        rc = verify_upstream_auth.main()
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
        rc = verify_upstream_auth.main()
        assert rc != 0
        captured = capsys.readouterr()
        # Key must never appear in the transport diagnostic.
        assert UPSTREAM_KEY not in captured.out
        assert UPSTREAM_KEY not in captured.err
        # And the bearer prefix is absent as well.
        assert "Bearer " not in captured.out
        assert "Bearer " not in captured.err
        # The error must still be observable.
        assert "transport" in captured.out.lower()


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
        with pytest.raises(SystemExit) as exc:
            verify_upstream_auth.main()
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "GOROUTER_TEST_UPSTREAM_KEY" in err

    def test_missing_openai_model_exits_2(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("GOROUTER_UPSTREAM_BASE_URL", "https://upstream.example.com")
        monkeypatch.setenv("GOROUTER_TEST_UPSTREAM_KEY", UPSTREAM_KEY)
        monkeypatch.setenv("GOROUTER_ANTHROPIC_MODEL", "claude-3-5-sonnet")
        with pytest.raises(SystemExit) as exc:
            verify_upstream_auth.main()
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "GOROUTER_OPENAI_MODEL" in err

    def test_missing_anthropic_model_exits_2(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("GOROUTER_UPSTREAM_BASE_URL", "https://upstream.example.com")
        monkeypatch.setenv("GOROUTER_TEST_UPSTREAM_KEY", UPSTREAM_KEY)
        monkeypatch.setenv("GOROUTER_OPENAI_MODEL", "gpt-4")
        with pytest.raises(SystemExit) as exc:
            verify_upstream_auth.main()
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "GOROUTER_ANTHROPIC_MODEL" in err


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
        rc = verify_upstream_auth.main()
        assert rc != 0
        captured = capsys.readouterr()
        assert UPSTREAM_KEY not in captured.out
        assert UPSTREAM_KEY not in captured.err
        assert "Bearer " + UPSTREAM_KEY not in captured.out
        assert "Bearer " + UPSTREAM_KEY not in captured.err
