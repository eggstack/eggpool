# Per-Account Outbound Proxies

The native Rust runtime supports a dedicated outbound proxy per account through
the in-process Eggress connector. A configured proxy is an explicit transport
choice; that account never silently falls back to direct transport.

## Configuration

Configure a named proxy under `[network.proxies]` and reference it from an
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
