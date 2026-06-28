# Network Diagnostics

EggPool keeps outbound HTTP clients alive and reuses connection pools for provider traffic. It also includes a small bounded DNS cache to reduce repeated resolver lookups on SBC deployments and local resolver setups such as Pi-hole. The cache is TTL-based and does not pin provider IP addresses permanently. Disable it with `[network.dns_cache].enabled = false` when debugging unusual DNS, VPN, or split-horizon behavior.

## Why a DNS cache?

On Raspberry Pi-class devices, every DNS lookup involves a system call and potentially a network round-trip to a local resolver (or Pi-hole). When EggPool makes repeated requests to the same upstream provider hosts, caching DNS results eliminates redundant lookups and reduces connection latency.

The cache is bounded (default 50 entries) and TTL-based (default 300s for positive results, 30s for negative results). It uses LRU eviction and singleflight deduplication so concurrent lookups for the same hostname share a single resolver call.

## Why long-lived outbound clients matter

Connection pooling alone reduces latency by reusing TLS sessions and TCP connections. The DNS cache complements this by avoiding redundant resolver calls, but the primary win comes from keeping HTTP clients alive across requests. `OutboundClientManager` builds one shared client at startup and reuses it for all non-provider network paths (update checks, external catalog fetches). The `build_count` metric should stabilize at 1; growth with request volume indicates a client lifecycle bug.

## Default configuration

```toml
[network]
# connect_timeout_s = 5
# read_timeout_s = 60
# max_connections = 20
# max_keepalive = 10
# keepalive_expiry_s = 30

[network.dns_cache]
enabled = true
# max_entries = 50
# positive_ttl_seconds = 300
# negative_ttl_seconds = 30
# stale_if_error_seconds = 3600
# prefer_ipv6 = false
# lookup_timeout_seconds = 5
```

## How to disable DNS caching

Set `enabled = false` in the `[network.dns_cache]` section:

```toml
[network.dns_cache]
enabled = false
```

This is useful when debugging DNS behavior, split-horizon DNS, VPN configurations, or when a local resolver requires fresh lookups for every connection.

## How to inspect DNS and cache behavior

### Dashboard

The Runtime dashboard page (`/runtime`) shows a Network section with:

- DNS cache enabled/disabled status
- Cache entries and hit rate
- Miss count and resolver errors
- Outbound client build count and request counts
- Provider client build count (per-provider)

### API

`GET /api/network/diagnostics` returns a sanitized JSON snapshot:

```json
{
  "outbound_clients": {
    "builds_total": 4,
    "scopes": {
      "global": 1,
      "provider:openai": 1,
      "provider:anthropic": 1,
      "provider:opencode-go": 1
    },
    "request_count": 1204,
    "error_count": 0,
    "has_client": true,
    "per_host_requests": {
      "pypi.org": 3,
      "api.github.com": 1
    },
    "per_host_errors": {}
  },
  "dns_cache": {
    "enabled": true,
    "max_entries": 50,
    "entries": 7,
    "hits_total": 1204,
    "misses_total": 8,
    "negative_hits_total": 0,
    "stale_hits_total": 0,
    "evictions_total": 0,
    "resolutions_total": 8,
    "errors_total": 0
  },
  "hosts": [
    {
      "host": "api.openai.com",
      "family": "ipv4",
      "state": "positive",
      "expires_in_seconds": 241.0,
      "stale_available": true,
      "last_error_kind": null
    },
    {
      "host": "api.anthropic.com",
      "family": "ipv4",
      "state": "negative",
      "expires_in_seconds": 30.0,
      "stale_available": false,
      "last_error_kind": "ConnectError"
    }
  ]
}
```

Key fields:

- **`outbound_clients.scopes`**: per-scope build counts. `global` is the shared `OutboundClientManager` client; `provider:*` entries are per-provider clients from `ProviderClientPool`.
- **`outbound_clients.per_host_requests`**: request counts by target host for the shared outbound client (update checks, catalog fetches).
- **`dns_cache.resolutions_total`**: cache misses that required a resolver refresh. Cache hits are reported separately and are not counted as DNS resolutions.
- **`hosts`**: per-cache-entry metadata including `state` (positive/negative), `expires_in_seconds` (TTL remaining), `stale_available` (stale-if-error window), and `last_error_kind` (for negative entries).

This endpoint is always auth-gated regardless of `dashboard.public` setting.

### CLI

`eggpool runtime-status` includes network diagnostics in its output:

```
Network:
  DNS cache:         enabled
  DNS entries:       7
  DNS hit rate:      99.3%
  DNS misses:        8
  DNS errors:        0
  Outbound builds:   1
  Outbound requests: 1204
  Outbound errors:   0
  Provider clients:  3
    anthropic: 1
    openai: 1
    opencode-go: 1
  DNS cache entries:
    api.openai.com (ipv4) state=positive expires=241s stale_ok
    api.anthropic.com (ipv4) state=negative expires=30s error=ConnectError
```

Use `--json` for machine-readable output.

## Pi-hole validation

When running EggPool on a host with Pi-hole or another DNS logger, you can validate that the DNS cache is working:

### Baseline

1. Disable the DNS cache: `[network.dns_cache].enabled = false`
2. Start EggPool
3. Send several requests to one or two providers
4. Check Pi-hole query count for the provider domains

### With cache enabled

1. Enable the DNS cache (default)
2. Send the same requests
3. Confirm Pi-hole query count drops significantly after the initial lookup
4. Confirm the hit rate on the Runtime dashboard is high
5. Confirm requests still succeed

### Cache expiry

The positive TTL is 300 seconds by default. After expiry, EggPool makes a fresh resolver call. If that refresh fails and the entry is still inside `stale_if_error_seconds`, EggPool temporarily reuses the previous addresses and records a stale hit. You can observe normal refreshes by watching the Pi-hole query count: it should spike briefly every 5 minutes for each cached host, then return to zero.

## Known caveats

- **CDNs**: some provider endpoints use CDN-backed hostnames that resolve to different IPs based on geographic location or load. The cache refreshes after the positive TTL, then uses stale addresses only if the refresh fails and the stale-if-error window is still open.
- **Split-horizon DNS**: if your DNS resolver returns different IPs based on the source host (common in corporate environments), the cache may store a private-network IP that becomes stale if the host moves between networks (e.g., VPN connect/disconnect). Disable the cache in this scenario.
- **VPNs**: similar to split-horizon DNS, VPN tunnel establishment changes the routing table but not cached DNS results. Restart EggPool after VPN state changes, or disable the cache.
- **Custom local DNS**: if you run a custom DNS server that performs filtering or logging, the cache reduces visibility into EggPool's DNS traffic. This is by design (the cache exists to reduce resolver load), but it means Pi-hole logs will show fewer queries than actual DNS lookups performed.
