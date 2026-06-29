"""Deterministic end-to-end test: transcoding is on by default.

These tests pin the canonical ``connect → configsetup opencode → POST
/v1/chat/completions → OpenAI response`` flow that the user reported as
broken. The OpenCode client always posts to ``/v1/chat/completions``;
when a configured provider only serves the Anthropic surface, the data
plane must translate the request and return an OpenAI-shaped response
without the operator setting ``[transcoder] enabled = true``.

The opt-out escape hatch (``enabled = false``) is also pinned to ensure
the legacy protocol-exact routing still works for operators who need it.

The fixtures use ``respx`` to mock the upstream so the test is
deterministic and offline. There is no real network or real provider
involvement.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import pytest_asyncio
import respx

from eggpool.accounts.registry import AccountRegistry
from eggpool.app import create_app
from eggpool.catalog.service import CatalogService
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import (
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
)
from eggpool.health.health_manager import HealthManager
from eggpool.models.config import AppConfig
from eggpool.request.coordinator import RequestCoordinator
from eggpool.routing.router import Router
from eggpool.transcoder.policy import TranscoderPolicy

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI


UPSTREAM_BASE = "https://api.minimax.io"
ANTHROPIC_PATH = "/anthropic/v1/messages"


def _build_minimax_only_config() -> AppConfig:
    """The user-reported scenario: Anthropic-only provider, no transcoder config.

    The user runs ``eggpool connect`` with a token-plan key for
    ``api.minimax.io`` and ends up with a single-protocol Anthropic
    provider. ``eggpool configsetup opencode`` then generates an OpenCode
    config that talks to ``/v1/chat/completions``. Without any explicit
    ``[transcoder]`` block, the OpenCode request to ``MiniMax-M3`` must
    be translated end-to-end.

    Note the absence of the ``transcoder`` key in the config dict — the
    default policy must kick in.
    """
    os.environ["MINIMAX_KEY"] = "test-minimax-token-plan-key"
    return AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "EGGPOOL_DEFAULT_KEY",
                "host": "127.0.0.1",
                "port": 0,
            },
            "database": {"path": ":memory:"},
            "upstream": {"base_url": UPSTREAM_BASE},
            "models": {
                "startup_refresh": False,
                "refresh_interval_s": 0,
                "collapse_models": False,
            },
            "providers": {
                "minimax": {
                    "id": "minimax",
                    "base_url": f"{UPSTREAM_BASE}/anthropic",
                    "protocols": ["anthropic"],
                    "anthropic_path": "/v1/messages",
                    "auth": {"mode": "api_key", "header": "x-api-key"},
                    "accounts": [
                        {"name": "mm-acct", "api_key_env": "MINIMAX_KEY"},
                    ],
                    "headers": [
                        {"name": "anthropic-version", "value": "2023-06-01"},
                    ],
                },
            },
            "dashboard": {"enabled": False},
        }
    )


@pytest.fixture(autouse=True)
def _set_test_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINIMAX_KEY", "test-minimax-token-plan-key")
    monkeypatch.setenv("EGGPOOL_DEFAULT_KEY", "eggpool-test-key")


@pytest_asyncio.fixture()
async def app() -> AsyncGenerator[FastAPI, None]:
    config = _build_minimax_only_config()

    # Sanity: confirm the test is exercising the default policy. If this
    # assertion ever fails the test has stopped being a "default"
    # assertion and must be updated.
    assert config.transcoder.enabled is True

    application = create_app(config)
    db = Database(path=config.database.path)
    await db.connect()
    application.state.db = db

    runner = MigrationRunner(db)
    await runner.run()

    async with db.transaction():
        await db.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("mm-acct", "MINIMAX_KEY"),
        )
        await db.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("MiniMax-M3", "anthropic"),
        )

    httpx_client = httpx.AsyncClient(
        base_url=config.upstream.base_url,
        timeout=httpx.Timeout(
            config.upstream.read_timeout_s,
            connect=config.upstream.connect_timeout_s,
            read=config.upstream.read_timeout_s,
            write=config.upstream.write_timeout_s,
            pool=config.upstream.keepalive_timeout_s,
        ),
        limits=httpx.Limits(
            max_connections=config.upstream.max_connections,
            max_keepalive_connections=config.upstream.max_keepalive,
            keepalive_expiry=config.upstream.keepalive_timeout_s,
        ),
    )
    application.state.httpx_client = httpx_client

    registry = AccountRegistry(config)
    application.state.registry = registry

    catalog = CatalogService(config, registry, db, httpx_client)
    application.state.catalog = catalog
    application.state.transcoder_policy = config.transcoder

    router = Router(registry, catalog, health_manager=HealthManager())
    application.state.router = router

    health_manager = HealthManager()
    application.state.health_manager = health_manager

    request_repo = RequestRepository(db)
    reservation_repo = ReservationRepository(db)
    attempt_repo = AttemptRepository(db)

    coordinator = RequestCoordinator(
        registry=registry,
        catalog=catalog,
        router=router,
        db=db,
        client_pool=httpx_client,
        request_repo=request_repo,
        reservation_repo=reservation_repo,
        attempt_repo=attempt_repo,
        health_manager=health_manager,
        config=config,
        transcoder_policy=config.transcoder,
    )
    application.state.coordinator = coordinator

    # Seed the catalog: MiniMax-M3 is an Anthropic-protocol model served
    # only by the mm-acct account under the minimax provider.
    catalog.cache.load_model(
        model_id="MiniMax-M3",
        display_name="MiniMax-M3",
        protocol="anthropic",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("MiniMax-M3", "mm-acct")
    catalog.cache.set_account_provider("mm-acct", "minimax")

    yield application

    await httpx_client.aclose()
    await db.disconnect()


@pytest_asyncio.fixture()
async def client(app: FastAPI) -> AsyncGenerator[httpx.AsyncClient, None]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as c:
        yield c


def _auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer eggpool-test-key"}


@pytest.mark.asyncio
async def test_default_policy_is_enabled_after_from_dict() -> None:
    """The default TranscoderPolicy constructed by AppConfig.from_dict is on.

    This pins the contract: a brand-new AppConfig that does NOT set
    ``transcoder`` must produce a policy with ``enabled = True``. If this
    test fails, every other test in this module becomes meaningless.
    """
    config = _build_minimax_only_config()
    assert isinstance(config.transcoder, TranscoderPolicy)
    assert config.transcoder.enabled is True
    assert config.transcoder.loss_policy == "warn"
    assert config.transcoder.prefer_native is True


@pytest.mark.asyncio
async def test_openai_chat_completions_to_anthropic_minimax_default(
    client: httpx.AsyncClient,
) -> None:
    """OpenCode POST /v1/chat/completions → Anthropic upstream, default policy.

    Reproduces the user-reported bug: with no ``[transcoder]`` block in
    config.toml, an OpenAI client calling ``MiniMax-M3`` (Anthropic
    protocol) on an Anthropic-only provider must still receive a valid
    OpenAI-shaped response. The upstream must see Anthropic-format JSON,
    and the client must see OpenAI-format JSON.
    """
    request_body = {
        "model": "MiniMax-M3",
        "messages": [{"role": "user", "content": "Hello from OpenCode"}],
        "temperature": 0.5,
        "max_tokens": 256,
    }

    upstream_calls: list[dict[str, Any]] = []

    def _capture(request: httpx.Request) -> httpx.Response:
        upstream_calls.append(
            {
                "url": str(request.url),
                "method": request.method,
                "headers": dict(request.headers),
                "body": json.loads(request.content) if request.content else {},
            }
        )
        return httpx.Response(
            200,
            json={
                "id": "msg_minimax_default_1",
                "type": "message",
                "role": "assistant",
                "model": "MiniMax-M3",
                "content": [{"type": "text", "text": "Hi there, OpenCode!"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 12, "output_tokens": 6},
            },
        )

    with respx.mock(assert_all_called=True) as mock:
        mock.post(f"{UPSTREAM_BASE}{ANTHROPIC_PATH}").mock(side_effect=_capture)

        response = await client.post(
            "/v1/chat/completions",
            json=request_body,
            headers=_auth_headers(),
        )

    assert response.status_code == 200, response.text
    assert len(upstream_calls) == 1, "expected exactly one upstream dispatch"

    # Upstream received Anthropic-format JSON (translation applied).
    sent = upstream_calls[0]
    assert sent["method"] == "POST"
    assert sent["body"]["model"] == "MiniMax-M3"
    # Anthropic expects a top-level `messages` array; the `system` prompt
    # would have been extracted from any messages[role=system] if present.
    assert sent["body"]["messages"] == [
        {"role": "user", "content": "Hello from OpenCode"}
    ]
    assert "max_tokens" in sent["body"]
    assert "temperature" in sent["body"]
    # Anthropic auth: x-api-key header (per the minimax provider config).
    assert sent["headers"].get("x-api-key") == "test-minimax-token-plan-key"
    assert sent["headers"].get("anthropic-version") == "2023-06-01"

    # Client received OpenAI-format JSON (response decoded back).
    body = response.json()
    assert body["object"] == "chat.completion"
    assert body["model"] == "MiniMax-M3"
    assert len(body["choices"]) == 1
    msg = body["choices"][0]["message"]
    assert msg["role"] == "assistant"
    assert msg["content"] == "Hi there, OpenCode!"
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"]["prompt_tokens"] == 12
    assert body["usage"]["completion_tokens"] == 6
    assert body["usage"]["total_tokens"] == 18


@pytest.mark.asyncio
async def test_default_policy_does_not_raise_protocol_mismatch(
    client: httpx.AsyncClient,
) -> None:
    """The exact error from the user's bug report must not occur by default.

    Before this fix, the default policy produced:

        Model 'MiniMax-M3' uses the Anthropic protocol. Use /v1/messages
        instead of /v1/chat/completions.

    With the default-on transcoder this error must never reach the
    client; the request must be translated and answered with 200.
    """
    request_body = {
        "model": "MiniMax-M3",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 32,
    }

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}{ANTHROPIC_PATH}").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "msg-1",
                    "type": "message",
                    "role": "assistant",
                    "model": "MiniMax-M3",
                    "content": [{"type": "text", "text": "pong"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 4, "output_tokens": 1},
                },
            )
        )
        response = await client.post(
            "/v1/chat/completions",
            json=request_body,
            headers=_auth_headers(),
        )

    assert response.status_code == 200, response.text
    body = response.json()
    # No ProtocolMismatchError leaks through.
    assert "error" not in body, body
    assert "Anthropic protocol" not in response.text
    assert "Use /v1/messages" not in response.text


@pytest.mark.asyncio
async def test_default_policy_handles_streaming_minimax(
    client: httpx.AsyncClient,
) -> None:
    """Streaming OpenCode request → Anthropic SSE → OpenAI SSE chunks.

    The streaming transcoder runs by default just like the body
    transcoder — no flag required. The client must see OpenAI
    ``data: {...}`` SSE chunks; the upstream must see Anthropic
    ``event: ...`` SSE chunks.
    """
    request_body = {
        "model": "MiniMax-M3",
        "messages": [{"role": "user", "content": "stream"}],
        "max_tokens": 32,
        "stream": True,
    }

    sse_chunks = [
        "event: message_start\n"
        'data: {"type":"message_start","message":{"id":"msg-1",'
        '"type":"message","role":"assistant","model":"MiniMax-M3",'
        '"content":[],"usage":{"input_tokens":4,"output_tokens":0}}}\n\n',
        "event: content_block_start\n"
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"text","text":""}}\n\n',
        "event: content_block_delta\n"
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"hello "}}\n\n',
        "event: content_block_delta\n"
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"world"}}\n\n',
        'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n',
        "event: message_delta\n"
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        '"usage":{"output_tokens":2}}\n\n',
        'event: message_stop\ndata: {"type":"message_stop"}\n\n',
    ]
    sse_payload = "".join(sse_chunks).encode()

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}{ANTHROPIC_PATH}").mock(
            return_value=httpx.Response(
                200,
                content=sse_payload,
                headers={"content-type": "text/event-stream"},
            )
        )
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json=request_body,
            headers=_auth_headers(),
        ) as response:
            assert response.status_code == 200, await response.aread()
            chunks: list[str] = []
            async for chunk in response.aiter_text():
                chunks.append(chunk)
            full = "".join(chunks)

    # OpenAI-style streaming markers present.
    assert "chat.completion.chunk" in full, full
    assert '"role": "assistant"' in full, full
    assert "hello " in full, full
    assert "world" in full, full
    # Anthropic-specific markers must NOT leak into the OpenAI stream.
    assert "event: content_block_delta" not in full
    assert "event: message_start" not in full
    # Stream terminates cleanly.
    assert full.rstrip().endswith("[DONE]") or '"finish_reason": "stop"' in full


@pytest.mark.asyncio
async def test_explicit_disabled_escape_hatch_blocks_translation(
    client: httpx.AsyncClient,
    app: FastAPI,
) -> None:
    """Setting [transcoder] enabled = false restores legacy behaviour.

    Operators who explicitly disable translation (deprecated escape
    hatch) must see the pre-default behaviour: a cross-protocol request
    fails with HTTP 400 ProtocolMismatchError, and the upstream is NOT
    contacted.
    """
    app.state.transcoder_policy = TranscoderPolicy(
        enabled=False,
        loss_policy="warn",
        prefer_native=True,
    )
    # Re-bind the coordinator's policy reference to the new policy.
    app.state.coordinator._transcoder_policy = app.state.transcoder_policy  # type: ignore[attr-defined]

    request_body = {
        "model": "MiniMax-M3",
        "messages": [{"role": "user", "content": "blocked"}],
        "max_tokens": 16,
    }

    upstream_was_called = False

    def _should_not_be_called(_request: httpx.Request) -> httpx.Response:
        nonlocal upstream_was_called
        upstream_was_called = True
        return httpx.Response(200, json={})

    with respx.mock:
        respx.post(f"{UPSTREAM_BASE}{ANTHROPIC_PATH}").mock(
            side_effect=_should_not_be_called
        )
        response = await client.post(
            "/v1/chat/completions",
            json=request_body,
            headers=_auth_headers(),
        )

    assert response.status_code == 400, response.text
    assert upstream_was_called is False
    body = response.json()
    assert "error" in body, body
    assert body["error"]["type"] == "invalid_request_error"
    assert "Anthropic protocol" in body["error"]["message"]
    assert "/v1/messages" in body["error"]["message"]


# ---------------------------------------------------------------------------
# CLI-level reproduction of the user's reported workflow:
# `eggpool connect` (token-plan API key) -> `eggpool configsetup opencode`.
#
# We exercise the non-interactive pieces (merge_provider_into_config and
# the Click `configsetup opencode` command) because the full interactive
# prompt loop is exercised elsewhere; the interesting invariant lives in
# the data plane, which is what the app-level tests above pin.
# ---------------------------------------------------------------------------


def test_cli_workflow_connect_then_configsetup_opencode_works(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """User flow: `connect` then `configsetup opencode` produces a working config.

    Reproduces the user-reported bug at the CLI level:

    1. Start with a minimal ``config.toml`` (no ``[transcoder]`` block,
       no providers).
    2. ``merge_provider_into_config`` — the same code path
       ``eggpool connect`` uses — adds an Anthropic-only ``minimax``
       provider with a token-plan API key.
    3. ``eggpool configsetup opencode`` prints the OpenCode snippet.
    4. The resulting snippet must be valid JSON, reference the
       ``/v1/chat/completions`` endpoint, and not depend on the
       operator setting ``[transcoder] enabled = true``.

    The data plane behaviour is pinned by the app-level tests above;
    this test pins the CLI surface so that future regressions in
    ``connect`` or ``configsetup opencode`` cannot reintroduce the bug.
    """
    import textwrap
    from unittest.mock import patch

    from click.testing import CliRunner

    from eggpool.cli_full import cli
    from eggpool.providers.connect import merge_provider_into_config

    monkeypatch.setenv("MINIMAX_KEY", "token-plan-api-key-123")
    monkeypatch.setenv("EGGPOOL_DEFAULT_KEY", "eggpool-cli-test-key")

    config_path = tmp_path / "config.toml"
    config_path.write_text(
        textwrap.dedent("""\
            [server]
            api_key = "eggpool-cli-test-key"
            port = 11300

            [models]
            collapse_models = false
        """)
    )

    # Step 2: simulate `eggpool connect` for the minimax token-plan key.
    ok = merge_provider_into_config(
        str(config_path),
        {
            "id": "minimax",
            "base_url": "https://api.minimax.io/anthropic",
            "protocols": ["anthropic"],
            "anthropic_path": "/v1/messages",
            "auth": {"mode": "api_key", "header": "x-api-key"},
            "headers": [
                {"name": "anthropic-version", "value": "2023-06-01"},
            ],
            "accounts": [
                {"name": "mm-acct", "api_key_env": "MINIMAX_KEY"},
            ],
        },
        "MINIMAX_KEY",
    )
    assert ok is True

    # Sanity: the user's config does NOT include a [transcoder] block.
    # Transcoding must work even when the block is absent — the data
    # plane default is the source of truth.
    config_text = config_path.read_text()
    assert "[transcoder]" not in config_text

    # Step 3: `eggpool configsetup opencode` must produce a valid JSON
    # snippet that the user can paste into OpenCode.
    runner = CliRunner()
    with patch("eggpool.providers.connect.restart_server", return_value=False):
        result = runner.invoke(
            cli, ["--config", str(config_path), "configsetup", "opencode"]
        )
    assert result.exit_code == 0, result.stdout + result.stderr
    snippet = json.loads(result.stdout)
    assert "provider" in snippet, snippet
    assert snippet["provider"]["eggpool"]["options"]["baseURL"].startswith("http")
    # The generated config points at /v1/chat/completions — the user's
    # OpenCode client always speaks OpenAI; transcoding handles the rest.
    assert "/v1" in snippet["provider"]["eggpool"]["options"]["baseURL"]
    assert "apiKey" in snippet["provider"]["eggpool"]["options"]

    # Final invariant: even after running `connect` and `configsetup`,
    # the operator never had to touch the [transcoder] block. The data
    # plane's default-enabled policy is what makes the bug-fixed flow
    # work end-to-end.
    from eggpool.models.config import AppConfig

    config_after = AppConfig.from_toml(str(config_path))
    assert config_after.transcoder.enabled is True
