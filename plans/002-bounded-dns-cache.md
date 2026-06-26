# Bounded DNS Cache Implementation Plan

## Context

Eggpool deployments may produce visible DNS traffic on networks that use Pi-hole or other local DNS logging. After outbound HTTP client lifecycle has been centralized and request paths reuse persistent connection pools, a small in-memory DNS cache is a reasonable additional optimization. The cache should reduce repeated resolver queries for the small set of provider/API hostnames eggpool actually needs, while preserving correct provider failover, CDN behavior, TLS SNI, and certificate validation.

The key principle is to cache DNS answers briefly, not to pin providers to IP addresses. Providers may use short TTLs, CDN load balancing, geo routing, failover, and multiple A/AAAA records. Eggpool should respect those semantics within a conservative bounded cache policy.

## Goals

- Add a bounded in-memory DNS cache suitable for SBC deployments.
- Default to a small maximum size, approximately 50 host entries.
- Respect DNS TTL where available, with a conservative upper cap.
- Add short negative caching to suppress resolver stampedes during transient failures.
- Optionally use stale known-good records during resolver errors, with strict bounds.
- Deduplicate concurrent lookups for the same hostname/address-family tuple.
- Integrate the resolver through the centralized outbound client manager.
- Preserve TLS SNI and certificate validation behavior.
- Add unit and integration tests for cache behavior.

## Non-goals

- Do not hardcode provider hostnames or IP addresses.
- Do not add static `/etc/hosts`-style provider mapping.
- Do not rewrite HTTPS URLs to numeric IP addresses at the request layer.
- Do not disable TLS verification.
- Do not implement a recursive resolver.
- Do not make eggpool responsible for provider-region selection.

## Configuration

Add a DNS cache section under the existing network configuration. Recommended default shape:

```toml
[network.dns_cache]
enabled = true
max_entries = 50
positive_ttl_seconds = 300
negative_ttl_seconds = 30
stale_if_error_seconds = 3600
```

Optional future fields, only if needed:

```toml
prefer_ipv6 = false
lookup_timeout_seconds = 5
```

Avoid adding too many knobs initially. The defaults should be safe for typical provider APIs and local resolver setups.

### Field semantics

`enabled`: enables or disables the cache. When disabled, resolution should fall back to the library/system resolver behavior.

`max_entries`: maximum number of cache keys. A key should include hostname and address-family policy. A value of 50 is intentionally small and should be enough for eggpool provider hosts.

`positive_ttl_seconds`: upper cap for positive DNS answers. If resolver TTLs are available, use `min(answer_ttl, positive_ttl_seconds)`. If answer TTLs are unavailable, use this value as the effective TTL.

`negative_ttl_seconds`: duration to cache lookup failures such as NXDOMAIN or temporary resolver failure. Keep this short so transient DNS problems recover quickly.

`stale_if_error_seconds`: maximum age for previously successful records that may be reused when fresh resolution fails. Use only for records that were once valid. Set to 0 to disable stale use.

## Architecture

Introduce a resolver abstraction owned by or injected into `OutboundClientManager`.

Suggested structure:

```text
OutboundClientManager
  ├── HttpClient(s)
  └── Resolver
        ├── SystemResolver or library-default resolver
        └── CachedResolver
              ├── bounded cache map
              ├── TTL policy
              ├── negative cache entries
              ├── stale-if-error support
              └── singleflight/concurrent lookup deduplication
```

The provider layer should not call the resolver directly. Provider code should continue to make normal HTTPS requests using hostnames. The HTTP transport layer should use the cached resolver while preserving the original hostname for SNI, certificate validation, `Host` header behavior, and request URL semantics.

## Cache key design

Use a key that is specific enough to avoid incorrect reuse but small enough to be effective:

```text
DnsCacheKey {
  hostname: normalized lowercase hostname,
  address_family: Any | IPv4 | IPv6,
}
```

Port generally should not affect DNS resolution, but if the selected HTTP library resolver API keys by `host:port`, retain port only at the adapter boundary, not in the logical DNS cache unless required.

Normalize hostnames consistently:

- Lowercase ASCII hostnames.
- Preserve IDNA/punycode behavior according to the existing URL parser/resolver library.
- Do not cache literal IP addresses as DNS entries.
- Do not cache empty, invalid, or redacted hostnames.

## Cache entry model

Use explicit entry types:

```text
Positive {
  addresses,
  expires_at,
  stale_until,
  original_ttl_observed_optional,
}

Negative {
  error_kind,
  expires_at,
}
```

Positive entries should contain all returned address candidates, not just one selected address. Preserve resolver ordering unless there is a deliberate policy to shuffle addresses. Avoid implementing custom load balancing in this phase.

Negative entries should contain coarse error kind only. Avoid storing sensitive request context. DNS hostnames are acceptable to store in memory and expose in debug output if redacted appropriately elsewhere.

## Concurrent lookup deduplication

Add singleflight-style deduplication. If multiple tasks request the same hostname while no valid cache entry exists, only one underlying resolver call should run. Other tasks should await the same result.

This matters because provider requests often arrive in bursts. Without deduplication, cache misses can still stampede the local resolver.

Acceptance behavior:

- Ten concurrent resolves for the same missing key cause one underlying resolver call.
- All ten callers receive the same result or equivalent cloned result.
- Failure is cached according to negative TTL.
- Cancellation of one waiter does not cancel the underlying lookup for all waiters unless all waiters are gone and the implementation safely supports that.

## Resolver integration options

Choose the implementation path that best matches the current eggpool HTTP stack.

### If the HTTP client exposes native DNS cache configuration

Prefer native support if it is robust and observable enough. Configure TTL and cache capacity centrally. Add wrapper metrics if possible. This is lower maintenance than custom resolver code.

### If the HTTP client supports custom resolver injection

Implement `CachedResolver` around the system/default async resolver and inject it into the HTTP client builder used by `OutboundClientManager`.

### If the HTTP client does not expose resolver hooks

Do not rewrite HTTPS requests to numeric IP URLs. Instead, either:

- Keep this phase as a no-op configuration layer until the HTTP stack can support resolver injection.
- Evaluate whether a different transport adapter can be used safely.
- Defer custom resolver work and document the limitation.

Correct TLS behavior is more important than DNS query reduction.

## Metrics

Add metrics with names adapted to existing conventions:

- `eggpool_dns_cache_hits_total{host,family}`
- `eggpool_dns_cache_misses_total{host,family}`
- `eggpool_dns_cache_negative_hits_total{host,family}`
- `eggpool_dns_cache_stale_hits_total{host,family}`
- `eggpool_dns_resolutions_total{host,family,result}`
- `eggpool_dns_resolution_errors_total{host,family,error_kind}`
- `eggpool_dns_cache_evictions_total{reason}`
- `eggpool_dns_cache_entries{state}` gauge if gauges are already used

If label cardinality is a concern, use provider name or a sanitized/allowlisted hostname label. Eggpool has a small provider hostname set, so host labels are probably acceptable, but this should be reviewed against the existing metrics style.

Do not include API keys, full URLs, query strings, headers, request bodies, or model prompts in DNS metrics.

## Implementation steps

1. Add DNS cache configuration to the config model and TOML parsing.
2. Add defaults and documentation for the DNS cache config.
3. Add resolver trait/interface or adapt to the HTTP library's resolver abstraction.
4. Implement an uncached system/default resolver wrapper.
5. Implement `CachedResolver` with positive, negative, and stale entries.
6. Add bounded eviction using LRU or equivalent recency policy.
7. Add TTL calculations with a positive TTL cap.
8. Add negative caching with short TTL.
9. Add stale-if-error behavior for previously successful records.
10. Add concurrent lookup deduplication.
11. Integrate the resolver into `OutboundClientManager`.
12. Wire metrics around cache hits, misses, resolutions, errors, stale hits, and evictions.
13. Add tests.
14. Add documentation and release notes.

## Tests

### Unit tests

- Positive lookup is cached until TTL expiry.
- Positive lookup expires and re-resolves after TTL.
- Effective TTL respects the configured cap.
- Negative lookup is cached for the negative TTL.
- Negative lookup expires and recovers after the resolver succeeds.
- Stale positive entry is used when fresh resolution fails and `stale_if_error_seconds > 0`.
- Stale positive entry is not used after stale window expires.
- LRU eviction removes older entries when `max_entries` is exceeded.
- Literal IP addresses bypass DNS cache behavior.
- Disabled cache delegates directly to underlying resolver.
- Concurrent lookups for the same host deduplicate underlying resolver calls.
- Concurrent lookups for different hosts do not block each other unnecessarily.

### Integration tests

- Repeated provider requests to the same host trigger one resolver lookup within the TTL window.
- Model discovery and provider forwarding share the same cache where appropriate.
- Cache expiry causes a subsequent resolver lookup.
- DNS resolver failure maps to the same user-visible provider/network error class as before, except where stale-if-error succeeds.
- HTTPS requests still use original hostnames for TLS validation.

TLS/SNI behavior may need a local test server or a mock transport depending on the HTTP stack.

## Documentation

Add a short section to the configuration documentation:

- Explain why eggpool has DNS caching.
- State that the cache is bounded and TTL-based.
- State that it does not pin provider IPs.
- Show how to disable it for debugging.
- Mention Pi-hole/local resolver deployments as a common motivation.

Example text:

```toml
[network.dns_cache]
enabled = true
max_entries = 50
positive_ttl_seconds = 300
negative_ttl_seconds = 30
stale_if_error_seconds = 3600
```

## Acceptance criteria

- DNS caching is enabled by default with a small bounded cache.
- Provider request URLs and TLS validation continue to use provider hostnames.
- Repeated requests to the same provider host produce cache hits within the TTL window.
- Cache entries expire and re-resolve.
- Resolver failures are not cached for long periods.
- Stale-if-error is bounded and observable.
- Concurrent cache misses for the same host are deduplicated.
- The cache can be disabled through config.
- Tests cover positive, negative, stale, eviction, disabled, and concurrent behavior.

## Risks and mitigations

Risk: DNS caching may interfere with provider/CDN failover.

Mitigation: Use conservative TTL caps and respect resolver TTLs where available. Keep the cache disableable.

Risk: Custom resolver integration may break TLS SNI/certificate behavior.

Mitigation: Integrate at the transport resolver layer, not by rewriting URLs to IP addresses.

Risk: Metrics may create high cardinality.

Mitigation: Hostnames are expected to be low cardinality in eggpool. If this changes, add host label sanitization or provider-level aggregation.

Risk: Negative caching may delay recovery from DNS problems.

Mitigation: Use short negative TTLs, defaulting to approximately 30 seconds.

Risk: Stale-if-error may hide resolver outages.

Mitigation: Expose stale hits as metrics/logs and bound the stale window.

## Handoff notes

This phase should follow the outbound client lifecycle cleanup. The DNS cache should be part of the centralized network/client layer, not individual provider implementations. If the current HTTP stack cannot support resolver injection safely, stop at config/docs/tests for the abstraction and document the blocker rather than implementing unsafe URL-to-IP rewriting.
