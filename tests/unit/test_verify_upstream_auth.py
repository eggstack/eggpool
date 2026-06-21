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

import json
import sys
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import verify_upstream_auth  # noqa: E402  (path setup in tests/conftest.py)

if TYPE_CHECKING:
    from pathlib import Path

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


class TestBearerPrefixRejection:
    """Bearer-prefixed keys must be rejected before any network call so the
    operator gets an actionable verifier error rather than a misleading
    upstream 401.
    """

    def _write_config(self, tmp_path: Path, *, api_key: str) -> Path:
        cfg_path: Path = tmp_path / "config.toml"
        cfg_path.write_text(
            f"""\
[providers.minimax]
id = "minimax"
base_url = "https://api.minimax.io/v1"
protocols = ["openai"]
openai_path = "/chat/completions"
models_path = "/models"

[[providers.minimax.accounts]]
name = "default"
api_key = "{api_key}"

[providers.minimax.auth]
mode = "bearer"

[providers.minimax.verify]
probe_model = "MiniMax-M2.5"
probe_protocol = "openai"
"""
        )
        return cfg_path

    def test_bearer_prefixed_inline_key_rejected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cfg_path = self._write_config(tmp_path, api_key="Bearer sk-test-123")
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "verify_upstream_auth",
                "--config",
                str(cfg_path),
                "--provider",
                "minimax",
            ],
        )

        called: dict[str, int] = {"count": 0}

        def _explode(request: httpx.Request) -> httpx.Response:
            called["count"] += 1
            return httpx.Response(200, content=b"{}")

        def _factory() -> httpx.Client:
            return httpx.Client(transport=httpx.MockTransport(_explode), timeout=5.0)

        monkeypatch.setattr(verify_upstream_auth, "_make_client", _factory)

        rc = verify_upstream_auth.main()
        assert rc != 0
        assert called["count"] == 0, (
            "verifier must reject bearer-prefixed key before any network call"
        )
        captured = capsys.readouterr()
        assert "raw key must not include Bearer prefix" in captured.out
        assert "sk-test-123" not in captured.out
        assert "sk-test-123" not in captured.err

    def test_bearer_prefixed_env_key_rejected(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("MINIMAX_KEY", "Bearer sk-test-123")
        cfg_path: Path = tmp_path / "config.toml"
        cfg_path.write_text(
            """\
[providers.minimax]
id = "minimax"
base_url = "https://api.minimax.io/v1"
protocols = ["openai"]
openai_path = "/chat/completions"
models_path = "/models"

[[providers.minimax.accounts]]
name = "default"
api_key_env = "MINIMAX_KEY"

[providers.minimax.auth]
mode = "bearer"

[providers.minimax.verify]
probe_model = "MiniMax-M2.5"
probe_protocol = "openai"
"""
        )
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "verify_upstream_auth",
                "--config",
                str(cfg_path),
                "--provider",
                "minimax",
            ],
        )

        called: dict[str, int] = {"count": 0}

        def _explode(request: httpx.Request) -> httpx.Response:
            called["count"] += 1
            return httpx.Response(200, content=b"{}")

        def _factory() -> httpx.Client:
            return httpx.Client(transport=httpx.MockTransport(_explode), timeout=5.0)

        monkeypatch.setattr(verify_upstream_auth, "_make_client", _factory)

        rc = verify_upstream_auth.main()
        assert rc != 0
        assert called["count"] == 0
        captured = capsys.readouterr()
        assert "raw key must not include Bearer prefix" in captured.out

    def test_raw_key_passes_bearer_prefix_check(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg_path: Path = tmp_path / "config.toml"
        cfg_path.write_text(
            f"""\
[providers.minimax]
id = "minimax"
base_url = "https://api.minimax.io/v1"
protocols = ["openai"]
openai_path = "/chat/completions"
models_path = "/models"

[[providers.minimax.accounts]]
name = "default"
api_key = "{UPSTREAM_KEY}"

[providers.minimax.auth]
mode = "bearer"
"""
        )

        state = _capture_transport()
        _install_mock_client(monkeypatch, state)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "verify_upstream_auth",
                "--config",
                str(cfg_path),
                "--provider",
                "minimax",
            ],
        )

        rc = verify_upstream_auth.main()
        assert rc == 0


class TestProviderVerifyConfigProbeModel:
    """The verifier consumes ``[providers.<id>.verify] probe_model`` and
    ``probe_protocol`` when CLI model flags are absent. CLI flags win.
    """

    def _write_minimax_config(self, tmp_path: Path, *, include_verify: bool) -> Path:
        cfg_path: Path = tmp_path / "config.toml"
        verify_block = ""
        if include_verify:
            verify_block = (
                "\n[providers.minimax.verify]\n"
                'probe_model = "MiniMax-M2.5"\n'
                'probe_protocol = "openai"\n'
            )
        cfg_path.write_text(
            f"""\
[providers.minimax]
id = "minimax"
base_url = "https://api.minimax.io/v1"
protocols = ["openai"]
openai_path = "/chat/completions"
models_path = "/models"

[[providers.minimax.accounts]]
name = "default"
api_key = "{UPSTREAM_KEY}"

[providers.minimax.auth]
mode = "bearer"
{verify_block}
"""
        )
        return cfg_path

    def _argv_for(self, cfg_path: Any, *, openai_model: str | None) -> list[str]:
        argv = [
            "verify_upstream_auth",
            "--config",
            str(cfg_path),
            "--provider",
            "minimax",
        ]
        if openai_model is not None:
            argv += ["--openai-model", openai_model]
        return argv

    def test_probe_model_used_when_cli_flag_absent(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg_path = self._write_minimax_config(tmp_path, include_verify=True)
        state = _capture_transport()
        _install_mock_client(monkeypatch, state)
        monkeypatch.setattr(sys, "argv", self._argv_for(cfg_path, openai_model=None))

        rc = verify_upstream_auth.main()
        assert rc == 0
        captured = state.get("openai")
        body_value: object = captured["body"]
        payload = json.loads(body_value)
        assert payload["model"] == "MiniMax-M2.5"

    def test_cli_openai_model_overrides_probe_model(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg_path = self._write_minimax_config(tmp_path, include_verify=True)
        state = _capture_transport()
        _install_mock_client(monkeypatch, state)
        monkeypatch.setattr(
            sys,
            "argv",
            self._argv_for(cfg_path, openai_model="MiniMax-OTHER"),
        )

        rc = verify_upstream_auth.main()
        assert rc == 0
        captured = state.get("openai")
        body_value: object = captured["body"]
        payload = json.loads(body_value)
        assert payload["model"] == "MiniMax-OTHER"

    def test_no_probe_model_skips_chat_probe(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg_path = self._write_minimax_config(tmp_path, include_verify=False)
        state = _capture_transport()
        _install_mock_client(monkeypatch, state)
        monkeypatch.setattr(sys, "argv", self._argv_for(cfg_path, openai_model=None))

        rc = verify_upstream_auth.main()
        assert rc == 0
        assert "openai" not in state.captured, (
            "no chat probe should run without --openai-model or probe_model"
        )

    def test_verbose_output_includes_resolved_url_and_auth_shape(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cfg_path = self._write_minimax_config(tmp_path, include_verify=True)
        state = _capture_transport()
        _install_mock_client(monkeypatch, state)
        monkeypatch.setattr(
            sys,
            "argv",
            self._argv_for(cfg_path, openai_model=None) + ["--verbose"],
        )

        rc = verify_upstream_auth.main()
        assert rc == 0
        captured = capsys.readouterr()
        assert "https://api.minimax.io/v1/chat/completions" in captured.out
        assert "Authorization: Bearer ***" in captured.out
        assert UPSTREAM_KEY not in captured.out

    def test_verbose_distinguishes_models_failure_from_chat_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        cfg_path: Path = tmp_path / "config.toml"
        cfg_path.write_text(
            f"""\
[providers.minimax]
id = "minimax"
base_url = "https://api.minimax.io/v1"
protocols = ["openai"]
openai_path = "/chat/completions"
models_path = "/models"

[[providers.minimax.accounts]]
name = "default"
api_key = "{UPSTREAM_KEY}"

[providers.minimax.auth]
mode = "bearer"

[providers.minimax.verify]
probe_model = "MiniMax-M2.5"
probe_protocol = "openai"
"""
        )

        state = _TransportState(openai_status=200)
        state.openai_status = 200
        state.openai_body = b'{"id":"x","choices":[]}'
        state.openai_request_id = "openai-req-1"
        state.anthropic_status = 200
        state.anthropic_body = b'{"id":"x","content":[]}'
        state.anthropic_request_id = "anthropic-req-1"

        def _handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/models"):
                return httpx.Response(500, content=b"upstream models failure")
            if path.endswith("/chat/completions"):
                return httpx.Response(
                    200,
                    headers={"x-request-id": "openai-req-1"},
                    content=b'{"id":"x","choices":[]}',
                )
            return httpx.Response(599, content=b"unrouted")

        def _factory() -> httpx.Client:
            return httpx.Client(transport=httpx.MockTransport(_handler), timeout=5.0)

        monkeypatch.setattr(verify_upstream_auth, "_make_client", _factory)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "verify_upstream_auth",
                "--config",
                str(cfg_path),
                "--provider",
                "minimax",
                "--verbose",
            ],
        )

        rc = verify_upstream_auth.main()
        assert rc != 0
        captured = capsys.readouterr()
        assert "models: " in captured.out
        assert "openai: " in captured.out
        assert "[FAIL]" in captured.out
        assert "[OK]" in captured.out
