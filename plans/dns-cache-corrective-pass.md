# DNS Cache Corrective Pass Plan

## Context

Eggpool recently added a bounded in-memory DNS cache, shared outbound client management, and network diagnostics for SBC/Pi-hole deployments. The implementation is structurally sound: provider clients and the shared outbound manager can use a `DnsNetworkBackend`, the cache has TTLs, negative entries, stale-if-error fallback, singleflight-style lookup deduplication, per-host counters, and dashboard/API/CLI diagnostics.

However, an observed runtime DNS cache hit rate of roughly 14% is lower than expected. That number may reflect a real missed optimization, but it may also reflect misleading accounting. The current metric shape can count singleflight waiters as ordinary misses, connection pooling reduces opportunities for cache hits, periodic model refreshes align with the default DNS TTL, and proxied account clients may bypass the cached network backend entirely.

This corrective pass should improve both actual DNS suppression and operator interpretation. The primary operational goal is not a high cache-hit percentage in isolation. The goal is fewer repeated resolver calls and fewer Pi-hole-visible DNS queries per upstream request, without breaking TLS, provider failover, proxy semantics, or privacy-sensitive remote-DNS behavior.

## Goals

- Improve actual DNS cache effectiveness for repeated provider/catalog/background traffic.
- Fix misleading DNS metrics, especially the treatment of singleflight waiters as misses.
- Expose resolver-call counts separately from cache-miss counts.
- Add a DNS suppression metric that better represents reduced resolver traffic.
- Avoid TTL synchronization with model refresh cadence.
- Identify and, where safe, close DNS-cache bypasses in proxied account transports.
- Preserve TLS SNI and certificate validation semantics.
- Preserve proxy DNS semantics by default, especially for proxy modes where remote DNS is desired.
- Improve dashboard/API/CLI diagnostics so low hit rate is actionable rather than ambiguous.
- Add tests that distinguish cache hits, owner misses, coalesced waits, actual resolver calls, and proxy bypass behavior.

## Non-goals

- Do not hardcode provider hostnames or IPs.
- Do not rewrite upstream URLs to numeric IPs at the application/request layer.
- Do not disable certificate verification.
- Do not force local DNS resolution for proxied accounts by default.
- Do not attempt to implement a recursive DNS resolver.
- Do not optimize for cache hit rate at the cost of provider/CDN failover correctness.
- Do not require Pi-hole or Raspberry Pi hardware in CI.

## Findings to address

### 1. Default TTL is synchronized with model refresh cadence

The default `models.refresh_interval_s` is 300 seconds. The default DNS `positive_ttl_seconds` is also 300 seconds. That means recurring model refreshes and pings can arrive just as DNS entries expire, producing misses instead of hits. For a small, provider-hostname-oriented cache, this is too conservative for the intended SBC use case.

### 2. Singleflight waiters are counted as misses

The current `DnsCache.resolve()` flow increments `misses` before determining whether the current caller owns the lookup or is waiting on an in-flight lookup for the same key. During a burst, ten simultaneous lookups for the same uncached hostname may count as ten misses even though only one underlying resolver call occurs. This depresses hit rate and obscures useful DNS suppression.

### 3. `resolutions_total` may be derived from misses

Diagnostics currently risk conflating misses with actual resolver calls. Resolver calls should mean calls to the underlying resolver, not every logical cache miss or singleflight waiter.

### 4. Proxied account clients bypass the cached backend

Provider clients without account proxy config can receive the shared DNS backend. Account-specific clients with `proxy_url` use `AsyncPProxyTransport`, which constructs its own `PProxyNetworkBackend`. That path does not currently use `DnsNetworkBackend`. Depending on proxy mode, this may be correct or may be a missed optimization. It must be made explicit.

### 5. Address-family diagnostics are misleading

The cache key uses `address_family`. The default is likely `AF_UNSPEC` when IPv6 is not preferred, but diagnostic formatting may label any non-`AF_INET` family as `ipv6`. `AF_UNSPEC` should be labeled as `any`, not `ipv6`.

### 6. Hit rate is the wrong primary operator metric

When HTTP keep-alive and connection pooling work well, fewer new TCP connections occur, so fewer DNS cache lookups occur. That can make cache hit rate look unimpressive even while resolver calls per upstream request are low. The dashboard should show resolver-call suppression, not just cache hit rate.

## Phase 1: Correct DNS cache counters

Refactor DNS cache metrics to distinguish logical cache interactions from real resolver work.

Add or clarify these counters on `DnsCache`:

```text
cache_hits
cache_misses_owner
singleflight_waits
negative_hits
stale_hits
resolver_calls
resolver_successes
resolver_errors
evictions_capacity
evictions_ttl_expiry
```

Recommended semantics:

- `cache_hits`: valid positive cache entry returned without resolver call.
- `cache_misses_owner`: no valid cache entry and this caller becomes the owner of the underlying resolver call.
- `singleflight_waits`: no valid cache entry, but another task is already resolving the same key; this caller waits for that result.
- `negative_hits`: valid negative cache entry raised without resolver call.
- `stale_hits`: stale positive entry returned after refresh attempt fails.
- `resolver_calls`: actual calls into the underlying DNS resolver, ideally incremented immediately before `socket.getaddrinfo` or equivalent.
- `resolver_successes`: underlying resolver returned at least one usable address.
- `resolver_errors`: underlying resolver failed or returned an empty response.

Backward compatibility can be preserved by continuing to expose legacy `hits` and `misses`, but new diagnostics should prefer the clearer fields. If `misses` remains exposed, define it as owner misses only, not waiters.

### Implementation detail

Move the miss increment so that it only occurs after singleflight ownership is known:

```text
if cache entry valid:
    hits += 1
    return addresses

if key in singleflight:
    singleflight_waits += 1
    return await future

cache_misses_owner += 1
singleflight[key] = future
resolver_calls += 1
perform lookup
```

This will make hit rate more meaningful and prevent burst traffic from being misrepresented as repeated DNS misses.

## Phase 2: Add derived DNS effectiveness metrics

Expose derived metrics in the cache snapshot and API/dashboard/CLI diagnostics.

Recommended derived values:

```text
logical_resolve_calls = cache_hits + cache_misses_owner + singleflight_waits + negative_hits + stale_hits
cache_hit_rate = cache_hits / max(1, cache_hits + cache_misses_owner)
dns_suppression_rate = (cache_hits + singleflight_waits + negative_hits + stale_hits) / max(1, logical_resolve_calls)
resolver_calls_per_logical_resolve = resolver_calls / max(1, logical_resolve_calls)
```

For dashboard purposes, the most useful operator-facing metrics are:

- Resolver calls.
- DNS suppression rate.
- Cache hits.
- Owner misses.
- Singleflight waits.
- Stale hits.
- Resolver errors.
- Entries/max entries.

Avoid presenting `cache_hit_rate` as the sole success criterion. Label it as cache reuse, not DNS suppression.

## Phase 3: Increase default positive TTL

Change the default positive DNS TTL from 300 seconds to 900 or 1800 seconds. Recommended default: 1800 seconds.

Rationale:

- Eggpool connects to a small, known set of provider and catalog hostnames.
- The cache is bounded and disableable.
- The current 300-second TTL aligns with the default model refresh interval and can cause systematic expiry just before recurring refreshes.
- Provider/CDN failover remains protected by a finite TTL and manual disablement.

Keep `negative_ttl_seconds = 30` and `stale_if_error_seconds = 3600` unless tests or deployment experience suggest otherwise.

Update all config examples, bundled config, README/config docs, `AGENTS.md`/skill notes if present, and tests that assert defaults.

Recommended example:

```toml
[network.dns_cache]
enabled = true
max_entries = 50
positive_ttl_seconds = 1800
negative_ttl_seconds = 30
stale_if_error_seconds = 3600
prefer_ipv6 = false
lookup_timeout_seconds = 5
```

## Phase 4: Fix address-family labeling and diagnostics

Add a helper for address-family labels and use it everywhere diagnostics or per-host keys are stringified.

Recommended mapping:

```text
AF_INET -> ipv4
AF_INET6 -> ipv6
AF_UNSPEC -> any
other -> family_<int>
```

Apply this to:

- `DnsCache.snapshot().by_host`.
- `DnsCache.snapshot().resolution_errors`.
- `DnsCache._snapshot_hosts()`.
- Dashboard rendering.
- CLI runtime/network status output.
- Tests.

This will prevent the default `AF_UNSPEC` path from being incorrectly shown as IPv6.

## Phase 5: Audit DNS-cache bypass paths

Perform a repository-wide audit for outbound HTTP/network paths that bypass the cached backend. Search for:

- `httpx.AsyncClient(`
- `httpx.Client(`
- `httpcore.AsyncConnectionPool(`
- `AsyncHTTPTransport(`
- `socket.getaddrinfo`
- `asyncio.open_connection`
- `pproxy`
- direct provider/catalog/update-check networking helpers

Classify each path as:

- Provider hot path.
- Provider model discovery/catalog path.
- External pricing catalog path.
- Update checker/background path.
- CLI-only path.
- Proxy transport path.
- Test-only path.

All non-proxy provider/background/catalog paths should use the shared cached backend when DNS caching is enabled. Test-only paths may remain isolated. CLI-only paths should use the outbound manager unless doing so would regress lightweight CLI startup work.

## Phase 6: Handle proxied accounts explicitly

Do not silently force local DNS for proxied accounts. Proxy DNS behavior is semantically meaningful:

- For some proxy types, remote DNS is desired for privacy and egress correctness.
- For LAN/SBC deployments using local account proxies, local cached DNS may be desirable.
- For pproxy transports, DNS may occur inside pproxy or on the proxy endpoint depending on protocol and implementation.

Add an explicit provider/account-level setting if local cached DNS through proxy transport is feasible:

```toml
[providers.example.accounts.some_account]
proxy_url = "socks5://127.0.0.1:1080"
proxy_dns_mode = "remote"        # default
# proxy_dns_mode = "local_cached" # opt-in, if supported safely
```

Possible enum:

```text
remote        # preserve current proxy-native behavior; default
local_cached  # resolve target host through Eggpool DNS cache, then connect proxy to resolved IP while preserving TLS SNI
```

Only implement `local_cached` if it can preserve TLS SNI and `Host` semantics. If pproxy cannot safely connect by resolved IP while allowing httpcore to preserve SNI/hostname, document the limitation and keep proxied accounts as an explicit bypass in diagnostics.

At minimum, diagnostics should report:

```text
provider_client_pool.proxy_clients_total
provider_client_pool.proxy_clients_dns_cache_mode.remote
provider_client_pool.proxy_clients_dns_cache_mode.local_cached
```

This makes it clear when low cache hit rate is expected because most traffic uses proxy transports outside the DNS cache.

## Phase 7: Improve dashboard/API/CLI presentation

Update `/api/network/diagnostics` to expose the new raw and derived DNS fields.

Recommended response shape additions:

```json
{
  "dns_cache": {
    "enabled": true,
    "max_entries": 50,
    "entries": 4,
    "cache_hits_total": 120,
    "cache_misses_owner_total": 8,
    "singleflight_waits_total": 32,
    "negative_hits_total": 0,
    "stale_hits_total": 0,
    "resolver_calls_total": 8,
    "resolver_successes_total": 8,
    "resolver_errors_total": 0,
    "cache_hit_rate": 0.9375,
    "dns_suppression_rate": 0.95,
    "resolver_calls_per_logical_resolve": 0.05,
    "by_host": {},
    "worst_missers": []
  }
}
```

Dashboard card should prioritize:

```text
DNS suppression: 95.0%
Resolver calls: 8
Cache hits: 120
Owner misses: 8
Coalesced waits: 32
Entries: 4 / 50
Errors: 0
```

CLI runtime status should mirror the same compact view.

Keep old fields for one release if they may be consumed externally, but mark them as legacy in docs if appropriate.

## Phase 8: Add worst-misser diagnostics

Add a derived `worst_missers` list sorted by owner misses and/or resolver calls.

Suggested shape:

```json
"worst_missers": [
  {
    "host": "api.example.com",
    "family": "any",
    "owner_misses": 8,
    "resolver_calls": 8,
    "hits": 120,
    "singleflight_waits": 32,
    "expires_in_seconds": 941.2
  }
]
```

This will quickly show whether low hit rate comes from:

- Many one-off hosts.
- One host expiring too often.
- Proxy-bypassed traffic.
- Negative lookups.
- Background catalog/update paths.

Limit this list to a small number, such as 10 or 20, to keep diagnostics compact.

## Phase 9: Tests

Add or update unit tests for `DnsCache`:

- Repeated sequential resolves produce one owner miss and subsequent hits.
- Concurrent resolves produce one owner miss, N-1 singleflight waits, and one resolver call.
- Singleflight waits are not counted as misses.
- Resolver failure increments resolver calls and resolver errors once for a burst.
- Negative cache hits are counted separately from owner misses.
- Stale-if-error increments stale hits and does not inflate cache hits.
- `AF_UNSPEC` is labeled as `any`.
- Derived rates are correct for zero-denominator and nonzero cases.
- TTL default is 1800 seconds or the chosen new default.

Add or update integration tests:

- `/api/network/diagnostics` exposes new fields and preserves legacy fields if retained.
- Dashboard rendering shows DNS suppression and resolver calls.
- CLI runtime/network status shows DNS suppression and resolver calls.
- Provider client pool diagnostics identify proxied clients and proxy DNS modes.
- Non-proxy provider clients still use `DnsNetworkBackend`.
- Proxied clients default to remote/proxy-native DNS behavior.
- Optional `local_cached` proxy DNS mode, if implemented, preserves TLS SNI/hostname behavior.

Avoid tests that depend on real network DNS.

## Phase 10: Manual validation procedure

After implementation, validate on the target Pi-hole/SBC setup.

Suggested procedure:

1. Restart eggpool with DNS cache enabled and positive TTL set to 1800 seconds.
2. Clear or mark Pi-hole logs.
3. Trigger startup model refresh.
4. Send repeated requests to one stable provider account for 5–10 minutes.
5. Record:
   - upstream request count,
   - DNS logical resolve count,
   - resolver calls,
   - DNS suppression rate,
   - cache hits,
   - owner misses,
   - singleflight waits,
   - Pi-hole query count for provider domains.
6. Repeat with account proxies enabled if proxies are part of the real deployment.
7. Compare proxied and non-proxied behavior.
8. Wait longer than the configured TTL and confirm re-resolution occurs.
9. Disable DNS cache and confirm resolver traffic returns to baseline.

Success should be measured by resolver calls and Pi-hole queries per upstream request, not by cache-hit rate alone.

## Acceptance criteria

- Singleflight waiters no longer count as ordinary cache misses.
- Actual resolver calls are counted separately and accurately.
- Dashboard/API/CLI expose DNS suppression rate and resolver calls.
- Default positive DNS TTL no longer aligns with the default 300-second model refresh interval.
- Address-family diagnostics label `AF_UNSPEC` as `any`.
- Proxied-account DNS behavior is explicit in config and/or diagnostics.
- Non-proxy provider clients and shared background/catalog clients continue to use the cached DNS backend when enabled.
- Tests cover counter semantics, derived rates, TTL defaults, address-family labels, diagnostics, and proxy behavior.
- Documentation explains that low cache-hit rate may be acceptable when connection pooling is working and that resolver calls per request/Pi-hole query count are the true operational signals.

## Suggested implementation order

1. Add address-family label helper and update tests.
2. Refactor DNS counters and singleflight accounting.
3. Add resolver-call and derived suppression metrics.
4. Update `/api/network/diagnostics`.
5. Update dashboard and CLI display.
6. Increase default TTL and update config/docs/tests.
7. Audit and document all bypass paths.
8. Add proxied-account diagnostics.
9. Optionally implement `proxy_dns_mode = "local_cached"` only if safe.
10. Run full unit/integration suite and perform manual Pi-hole validation.

## Notes for implementers

The cache should remain small and conservative. A larger TTL improves hit rate, but the implementation must remain easy to disable for VPN, split-horizon DNS, provider CDN, or failover debugging. The corrective pass should avoid optimizing the visible hit-rate number in isolation. The more important regression tests are that resolver calls are suppressed, connection reuse remains intact, TLS/SNI behavior remains correct, and proxy DNS semantics remain explicit.
