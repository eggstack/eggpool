"""T001 contract and deterministic fixture tests."""

from __future__ import annotations

import asyncio
import json
import os
import ssl
from pathlib import Path

import httpx
import pytest

from eggpool.errors import ConfigError
from eggpool.models.config import AccountConfig, AppConfig, ProviderConfig
from eggpool.providers.client_pool import ProviderClientPool
from eggpool.providers.contract import compose_provider_url
from tests.migration_rs.provider_transport_fixtures import (
    HTTPConnectProxy,
    RecordingHTTPServer,
    SOCKS5Proxy,
    TransportErrorObservation,
    TransportResponse,
    observe_transport_error,
    redact_proxy_uri,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures"
TLS_CERTIFICATE = FIXTURE_ROOT / "tls" / "localhost.crt"
TLS_PRIVATE_KEY = FIXTURE_ROOT / "tls" / "localhost.key"
CORPUS_PATH = (
    Path(__file__).parents[2]
    / "migration-rs"
    / "fixtures"
    / "provider-transport"
    / "proxy-capability-corpus.json"
)


def _client_kwargs() -> dict[str, object]:
    return {
        "timeout": httpx.Timeout(connect=1.0, read=1.0, write=1.0, pool=0.2),
        "limits": httpx.Limits(
            max_connections=2,
            max_keepalive_connections=1,
            keepalive_expiry=10.0,
        ),
        "trust_env": False,
        "follow_redirects": False,
    }


def _tls_client_context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(TLS_CERTIFICATE))


@pytest.mark.asyncio
async def test_http_fixture_records_request_shape_and_chunked_body() -> None:
    with RecordingHTTPServer(
        {
            ("POST", "/probe"): TransportResponse(
                chunks=(b"first", b"second"),
                delay_between_chunks_s=0.01,
            )
        }
    ) as server:
        async with httpx.AsyncClient(**_client_kwargs()) as client:
            response = await client.post(
                f"{server.base_url}/probe?ignored=1",
                content=b"synthetic request body",
                headers={"x-contract": "fixture"},
            )
            body = b"".join([part async for part in response.aiter_bytes()])

    assert response.status_code == 200
    assert body == b"firstsecond"
    assert server.connections_opened == 1
    assert len(server.requests) == 1
    request = server.requests[0]
    assert request.method == "POST"
    assert request.path == "/probe"
    assert request.body_length == len(b"synthetic request body")
    assert "x-contract" in request.header_names
    assert "authorization" not in request.header_names


@pytest.mark.asyncio
async def test_direct_http_reuses_http11_connection() -> None:
    with RecordingHTTPServer(
        {
            ("GET", "/one"): TransportResponse(body=b"one"),
            ("GET", "/two"): TransportResponse(body=b"two"),
        }
    ) as server:
        async with httpx.AsyncClient(**_client_kwargs()) as client:
            first = await client.get(f"{server.base_url}/one")
            second = await client.get(f"{server.base_url}/two")

    assert (first.text, second.text) == ("one", "two")
    assert server.connections_opened == 1
    assert [request.connection_id for request in server.requests] == [1, 1]


@pytest.mark.asyncio
async def test_direct_https_uses_test_ca_without_disabling_verification() -> None:
    with RecordingHTTPServer(
        {("GET", "/secure"): TransportResponse(body=b"tls-ok")},
        tls_certificate=str(TLS_CERTIFICATE),
        tls_private_key=str(TLS_PRIVATE_KEY),
    ) as server:
        async with httpx.AsyncClient(
            **_client_kwargs(), verify=_tls_client_context()
        ) as client:
            response = await client.get(f"{server.base_url}/secure")

    assert response.status_code == 200
    assert response.text == "tls-ok"
    assert server.requests[0].path == "/secure"


def test_provider_url_join_and_timeout_guardrail_are_frozen() -> None:
    provider = ProviderConfig(
        id="provider",
        base_url="https://provider.example/v1/",
        read_timeout_s=10,
        stream_timeouts={"first_byte_timeout_s": 20, "idle_timeout_s": 40},
    )
    assert compose_provider_url(provider, "/chat/completions") == (
        "https://provider.example/v1/chat/completions"
    )
    assert (
        provider.stream_timeouts.transport_read_timeout(provider.read_timeout_s) == 40
    )

    pool = ProviderClientPool.from_config({"provider": provider})
    client = pool.get_client("provider")
    assert client.timeout.connect == 5
    assert client.timeout.read == 40
    assert client.timeout.write == 30
    assert client.timeout.pool == 30
    assert client._trust_env is False


def test_provider_pool_topology_and_snapshot_are_contractual() -> None:
    pool = ProviderClientPool()
    provider_client = httpx.AsyncClient(base_url="https://provider.example")
    account_client = httpx.AsyncClient(base_url="https://provider.example")
    pool.register("provider", provider_client)
    pool.register_account("provider", "proxied", account_client)

    assert pool.get_client("provider", "direct") is provider_client
    assert pool.get_client("provider", "proxied") is account_client
    assert pool.snapshot() == {
        "build_count": 2,
        "providers": {"provider": 2},
        "account_client_count": 1,
        "account_clients": [{"provider_id": "provider", "account_name": "proxied"}],
    }

    asyncio.run(pool.close())
    assert provider_client.is_closed and account_client.is_closed


def _config_with_account(
    account: AccountConfig, *, proxies: dict[str, object] | None = None
) -> AppConfig:
    if account.api_key is None and not account.api_key_env:
        account = account.model_copy(update={"api_key": "synthetic-api-key"})
    return AppConfig(
        proxies=proxies or {},
        providers={
            "provider": ProviderConfig(
                id="provider",
                base_url="https://provider.example",
                accounts=[account],
            )
        },
    )


def test_proxy_resolution_precedence_and_trimming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ACCOUNT_PROXY", "  socks5://account.example:1080  ")
    config = _config_with_account(
        AccountConfig(name="account", proxy_url_env="ACCOUNT_PROXY")
    )
    account = config.providers["provider"].accounts[0]
    assert config.resolve_account_proxy_url(account) == "socks5://account.example:1080"

    named = _config_with_account(
        AccountConfig(name="named", proxy="shared"),
        proxies={"shared": {"url": "http://proxy.example:8080"}},
    )
    named_account = named.providers["provider"].accounts[0]
    assert named.resolve_account_proxy_url(named_account) == "http://proxy.example:8080"


@pytest.mark.parametrize(
    ("value", "message"),
    [(None, "not set"), ("", "not set"), (" \t\n ", "whitespace-only")],
)
def test_proxy_environment_failure_matrix(
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
    message: str,
) -> None:
    if value is None:
        monkeypatch.delenv("MISSING_PROXY", raising=False)
    else:
        monkeypatch.setenv("MISSING_PROXY", value)
    config = _config_with_account(
        AccountConfig(name="account", proxy_url_env="MISSING_PROXY")
    )
    account = config.providers["provider"].accounts[0]
    with pytest.raises(ConfigError, match=message):
        config.resolve_account_proxy_url(account)


def test_proxy_mutual_exclusion_and_unknown_named_proxy_fail_closed() -> None:
    with pytest.raises(ConfigError, match="at most one"):
        AccountConfig(name="account", proxy="named", proxy_url="http://proxy")
    with pytest.raises(ConfigError, match="unknown proxy"):
        _config_with_account(AccountConfig(name="account", proxy="missing"))


def _proxy_transport(proxy_uri: str, *, tls: bool = False) -> httpx.AsyncBaseTransport:
    pytest.importorskip("pproxy")
    from eggpool.providers.pproxy_transport import AsyncPProxyTransport

    context = ssl.create_default_context(cafile=str(TLS_CERTIFICATE)) if tls else None
    return AsyncPProxyTransport(
        proxy_uri,
        limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
        ssl_context=context,
    )


@pytest.mark.asyncio
async def test_python_pproxy_http_connect_and_https_observations() -> None:
    pytest.importorskip("pproxy")
    with (
        RecordingHTTPServer(
            {("GET", "/through"): TransportResponse(body=b"proxied")}
        ) as http_server,
        RecordingHTTPServer(
            {("GET", "/through"): TransportResponse(body=b"proxied")},
            tls_certificate=str(TLS_CERTIFICATE),
            tls_private_key=str(TLS_PRIVATE_KEY),
        ) as server,
        HTTPConnectProxy(username="proxy-user", password="proxy-pass") as proxy,
    ):
        proxy_uri = proxy.uri + "#proxy-user:proxy-pass"
        transport = _proxy_transport(proxy_uri)
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            response = await client.get(f"http://localhost:{http_server.port}/through")
        await transport.aclose()

        tls_transport = _proxy_transport(proxy_uri, tls=True)
        async with httpx.AsyncClient(
            transport=tls_transport, trust_env=False
        ) as client:
            tls_response = await client.get(f"https://localhost:{server.port}/through")
        await tls_transport.aclose()

    assert response.text == "proxied"
    assert tls_response.text == "proxied"
    assert len(proxy.observations) == 2
    assert all(item.protocol == "http_connect" for item in proxy.observations)
    assert all(item.authenticated for item in proxy.observations)
    assert all(item.target_host == "localhost" for item in proxy.observations)
    assert len(http_server.requests) == 1
    assert len(server.requests) == 1


@pytest.mark.asyncio
async def test_python_pproxy_socks5_auth_preserves_domain_target() -> None:
    pytest.importorskip("pproxy")
    with (
        RecordingHTTPServer(
            {("GET", "/through"): TransportResponse(body=b"socks-ok")}
        ) as server,
        SOCKS5Proxy(username="proxy-user", password="proxy-pass") as proxy,
    ):
        transport = _proxy_transport(proxy.uri + "#proxy-user:proxy-pass")
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            response = await client.get(f"http://localhost:{server.port}/through")
        await transport.aclose()

    assert response.text == "socks-ok"
    assert len(proxy.observations) == 1
    assert proxy.observations[0].target_address_kind == "domain"
    assert proxy.observations[0].target_host == "localhost"
    assert proxy.observations[0].authenticated


@pytest.mark.asyncio
async def test_configured_proxy_failure_never_falls_back_to_direct() -> None:
    pytest.importorskip("pproxy")
    with (
        RecordingHTTPServer(
            {("GET", "/never-direct"): TransportResponse(body=b"must-not-arrive")}
        ) as server,
        HTTPConnectProxy() as proxy,
    ):
        proxy_uri = proxy.uri
    transport = _proxy_transport(proxy_uri)
    try:
        async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
            with pytest.raises(httpx.HTTPError):
                await client.get(f"http://localhost:{server.port}/never-direct")
    finally:
        await transport.aclose()
    assert server.requests == []


def test_transport_errors_are_structural_and_secret_safe() -> None:
    secret = "synthetic-proxy-secret-7f4e"
    uri = f"http://proxy-user:{secret}@proxy.example:3128#{secret}"
    observation = observe_transport_error(
        httpx.ConnectError(secret), stage="connect", proxy_uri=uri
    )
    assert isinstance(observation, TransportErrorObservation)
    rendered = json.dumps(observation.to_dict(), sort_keys=True)
    assert secret not in rendered
    assert redact_proxy_uri(uri) == "http://proxy.example:3128"
    assert observation.to_dict() == {
        "category": "connect_error",
        "stage": "connect",
        "network_path": "proxied",
        "proxy_endpoint": "http://proxy.example:3128",
    }


@pytest.mark.asyncio
async def test_delayed_and_malformed_fixtures_preserve_failure_stage() -> None:
    with RecordingHTTPServer(
        {
            ("GET", "/delayed"): TransportResponse(delay_before_headers_s=0.15),
            ("GET", "/closed"): TransportResponse(close_without_response=True),
            ("GET", "/malformed"): TransportResponse(malformed_bytes=b"not-http"),
        }
    ) as server:
        timeout = httpx.Timeout(connect=1.0, read=0.03, write=1.0, pool=0.2)
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            with pytest.raises(httpx.ReadTimeout) as delayed:
                await client.get(f"{server.base_url}/delayed")
            with pytest.raises(httpx.RemoteProtocolError) as malformed:
                await client.get(f"{server.base_url}/malformed")
            with pytest.raises(httpx.RemoteProtocolError) as closed:
                await client.get(f"{server.base_url}/closed")

    delayed_observation = observe_transport_error(delayed.value, stage="read")
    malformed_observation = observe_transport_error(malformed.value, stage="protocol")
    closed_observation = observe_transport_error(closed.value, stage="protocol")
    assert delayed_observation.category == "read_timeout"
    assert malformed_observation.category == "protocol_error"
    assert closed_observation.category == "protocol_error"


def test_error_class_or_network_path_changes_are_not_normalized() -> None:
    direct = observe_transport_error(httpx.ConnectTimeout("synthetic"), stage="connect")
    proxied = observe_transport_error(
        httpx.ConnectError("synthetic"),
        stage="connect",
        proxy_uri="socks5://proxy.example:1080",
    )
    assert direct.to_dict() != proxied.to_dict()
    assert direct.category == "connect_timeout"
    assert proxied.network_path == "proxied"


def test_proxy_capability_corpus_is_machine_readable_and_feature_justified() -> None:
    document = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    rows = document["rows"]
    required = {
        "id",
        "uri",
        "python_parse",
        "python_connection",
        "egress_construction",
        "egress_runtime",
        "required_egress_features",
        "parity_class",
        "mandatory_for_rust_cutover",
    }
    assert rows
    assert all(required <= set(row) for row in rows)
    enabled = set(document["egress_feature_decision"]["enabled_features"])
    justified = {
        feature
        for row in rows
        if row["mandatory_for_rust_cutover"]
        for feature in row["required_egress_features"]
    }
    assert enabled <= justified
    assert not enabled & set(document["egress_feature_decision"]["rejected_features"])


def test_tls_fixture_is_not_a_production_trust_override() -> None:
    context = ssl.create_default_context(cafile=str(TLS_CERTIFICATE))
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert os.path.basename(TLS_CERTIFICATE) == "localhost.crt"
