# Per-Account Outbound Proxies

The native Rust runtime supports a dedicated outbound proxy per account through
the in-process Eggress connector. A configured proxy is an explicit transport
choice; that account never silently falls back to direct transport.

Proxy values use pproxy URI syntax. The supported outbound surface includes
HTTP CONNECT, SOCKS4, SOCKS5, Shadowsocks, Trojan, explicit `direct://`, and
multi-hop chains joined with `__`. SSH is supported as an upstream chain hop
in the default build through the `eggress-ssh-fallback` compatibility feature;
a deliberate `--no-default-features` build rejects SSH proxy configuration at
construction time while retaining the other proxy protocols.
The implementation keeps these protocols in the native Eggress feature graph
and applies the same provider TLS, timeout, and retry ownership after the
proxy connection is established. In a no-default build, the graph omits the
optional SSH compatibility implementation; configured SSH is rejected rather
than silently bypassed.

## Configuration

Configure a named proxy under `[proxies]` and reference it from an
account with `proxy = "name"`. Keep credentials in environment variables where
possible and use a disposable configuration for connectivity checks.

```toml
[network.proxies.egress]
url = "socks5://127.0.0.1:1080"

[providers.example.accounts]
# proxy = "egress"
```

Validate and inspect the resulting account topology with the native CLI:

```bash
eggpool --config config.toml check-config
eggpool --config config.toml accounts status
```

Proxy connection, TLS, timeout, and protocol failures are classified by the
provider transport and surfaced through the ordinary health/retry policy.
