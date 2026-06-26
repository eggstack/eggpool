# Network Diagnostics and SBC Validation Plan

## Context

After centralizing outbound HTTP client lifecycle and adding a bounded DNS cache, eggpool needs enough diagnostics to prove the changes work on the intended deployment target: small SBC systems, often Raspberry Pi-class devices, sometimes with Pi-hole or another local DNS resolver/logger on the same LAN. The implementation should make DNS and outbound-network behavior visible without exposing credentials, prompts, or full provider request data.

This plan closes the DNS/network optimization line by adding dashboard/API visibility, CLI diagnostics if appropriate, documentation, and practical validation criteria.

## Goals

- Expose enough diagnostics to confirm DNS cache effectiveness.
- Expose enough diagnostics to confirm outbound client reuse.
- Add SBC/Pi-hole validation notes for maintainers and testers.
- Document safe configuration and troubleshooting behavior.
- Ensure logs and metrics do not leak API keys, request bodies, prompts, or full URLs.
- Provide clear acceptance criteria for closing the performance hardening work.

## Non-goals

- Do not build a large network observability subsystem.
- Do not add packet capture or privileged network inspection.
- Do not expose provider API keys, authorization headers, prompts, completions, or full request URLs.
- Do not require Pi-hole for normal tests or CI.
- Do not require Raspberry Pi hardware for CI.

## Diagnostics model

Add a compact network diagnostics surface that summarizes current outbound client and DNS cache state. Depending on existing eggpool conventions, expose this through one or more of:

- Metrics endpoint.
- Existing dashboard API.
- Dashboard network/status panel.
- CLI diagnostic command.
- Debug-level logs.

The metrics endpoint should be the source of truth if eggpool already has a metrics system. The dashboard can consume the same counters or an aggregated diagnostics endpoint.

## Required metrics

Use existing naming conventions if they differ, but the diagnostic content should include the following concepts.

### Outbound client metrics

- Outbound request count by provider, host, method, and status class.
- Outbound request errors by provider, host, and coarse error kind.
- Outbound request duration if histograms are already used.
- Outbound client construction count by scope.

Suggested names:

```text
eggpool_outbound_requests_total{provider,host,method,status_class}
eggpool_outbound_request_errors_total{provider,host,error_kind}
eggpool_outbound_request_duration_seconds{provider,host}
eggpool_outbound_client_builds_total{scope}
```

The most important validation signal is that `eggpool_outbound_client_builds_total` does not increase with forwarded request volume after startup and provider initialization.

### DNS cache metrics

- DNS cache hits.
- DNS cache misses.
- Negative cache hits.
- Stale-if-error hits.
- Underlying resolver calls.
- Resolver errors.
- Cache evictions.
- Current cache entries, if gauges are available.

Suggested names:

```text
eggpool_dns_cache_hits_total{host,family}
eggpool_dns_cache_misses_total{host,family}
eggpool_dns_cache_negative_hits_total{host,family}
eggpool_dns_cache_stale_hits_total{host,family}
eggpool_dns_resolutions_total{host,family,result}
eggpool_dns_resolution_errors_total{host,family,error_kind}
eggpool_dns_cache_evictions_total{reason}
eggpool_dns_cache_entries{state}
```

If host labels are considered too granular, aggregate by provider and address family. Eggpool is expected to resolve a small set of provider API hosts, so host labels are acceptable if sanitized and bounded.

## Diagnostics endpoint

If eggpool has an internal API/dashboard API, add a compact endpoint similar to:

```text
GET /api/network/diagnostics
```

Suggested response shape:

```json
{
  "outbound_clients": {
    "builds_total": 4,
    "scopes": {
      "global": 1,
      "provider": 3
    }
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
      "host": "api.example-provider.com",
      "family": "any",
      "state": "positive",
      "expires_in_seconds": 241,
      "stale_available": true,
      "last_error_kind": null
    }
  ]
}
```

This endpoint should not expose:

- API keys.
- Authorization headers.
- Request headers.
- Request bodies.
- Prompt/completion content.
- Query strings.
- Full upstream URLs.

Hostnames may be shown because they are operationally useful and low sensitivity in this context. If a provider configuration allows arbitrary private hosts, consider redacting hostnames by default and exposing them only in debug mode.

## Dashboard panel

If the dashboard already has a system/status area, add a small `Network` or `Resolver` section. Keep it compact:

- DNS cache enabled/disabled.
- Cache entries/max entries.
- Hit rate.
- Miss count.
- Resolver errors.
- Stale hits.
- Outbound client builds.

Avoid a large table unless the dashboard already has a diagnostics tab. A detailed host table can be placed behind an expandable section.

Suggested display:

```text
Network
DNS cache: enabled
Entries: 7 / 50
Hit rate: 99.3%
Resolver calls: 8
Resolver errors: 0
Stale hits: 0
Outbound client builds: 4
```

## CLI diagnostics

If eggpool has a suitable CLI command namespace, add one lightweight command such as:

```text
eggpool diagnostics network
```

or:

```text
eggpool status --network
```

The CLI should print a sanitized summary equivalent to the diagnostics endpoint. Do not make this command import or initialize heavyweight server modules if prior CLI startup cost work is in progress. If a lightweight CLI command would conflict with CLI optimization work, skip the CLI and rely on the API/dashboard/metrics path.

## Logging

Add debug-level logs for relevant lifecycle events:

- Outbound client manager initialization.
- DNS cache enabled/disabled and effective config.
- DNS cache eviction events at trace/debug level.
- Resolver errors with sanitized host and coarse error kind.
- Stale-if-error usage.

Do not log every cache hit at normal levels. That would create unnecessary log noise and filesystem writes on SBC deployments. Cache hit logging, if present, should be trace-only.

## Documentation

Add or update documentation covering:

- Why eggpool has a DNS cache.
- Why long-lived outbound clients matter more than DNS caching alone.
- Default DNS cache config.
- How to disable DNS caching.
- How to inspect DNS/cache behavior.
- How to validate with Pi-hole.
- Known caveats around CDNs, split-horizon DNS, VPNs, and custom local DNS.

Suggested documentation language:

```markdown
Eggpool keeps outbound HTTP clients alive and reuses connection pools for provider traffic. It also includes a small bounded DNS cache to reduce repeated resolver lookups on SBC deployments and local resolver setups such as Pi-hole. The cache is TTL-based and does not pin provider IP addresses permanently. Disable it with `[network.dns_cache].enabled = false` when debugging unusual DNS, VPN, or split-horizon behavior.
```

## SBC/Pi-hole validation procedure

Perform manual validation on a Raspberry Pi or equivalent small Linux host where possible. Pi-hole is useful but not mandatory.

### Baseline

1. Run eggpool before the changes or with DNS cache disabled.
2. Clear or mark Pi-hole logs if possible.
3. Start eggpool.
4. Trigger provider model discovery.
5. Send repeated requests to one or two providers.
6. Let update/background checks run or trigger equivalent commands.
7. Record DNS query count from eggpool host, grouped by domain.
8. Record request latency qualitatively or through existing metrics.
9. Record CPU spikes if visible.

### After client lifecycle cleanup

1. Run the same workload with centralized outbound clients but DNS cache disabled.
2. Confirm outbound client build count remains stable.
3. Compare Pi-hole DNS volume against baseline.
4. If DNS volume drops substantially, note that client churn was the primary cause.

### After DNS cache enabled

1. Enable DNS cache with defaults.
2. Run the same workload.
3. Confirm DNS cache hit rate rises after initial discovery.
4. Confirm Pi-hole DNS query count drops for repeated provider requests.
5. Confirm resolver calls occur again after TTL expiry.
6. Confirm provider requests continue to succeed.
7. Confirm dashboard/API/metrics reflect cache behavior.

### Failure-mode validation

Where practical:

- Temporarily break resolver access and confirm negative cache behavior is short-lived.
- Restore resolver access and confirm recovery occurs after negative TTL expiry.
- Confirm stale-if-error usage is counted if enabled and applicable.
- Disable DNS cache and confirm direct resolver behavior returns.

Do not require destructive network changes in automated tests.

## Automated tests

Add tests for diagnostics separately from resolver unit tests:

- Metrics counters increment on cache hit/miss/resolution/error paths.
- Diagnostics endpoint redacts or omits sensitive fields.
- Diagnostics endpoint reports enabled/disabled state correctly.
- Dashboard/API aggregation handles empty cache state.
- Outbound client build count is exposed and stable across mock requests.
- CLI diagnostics, if implemented, does not print secrets.

Use mock providers and mock resolvers. CI should not depend on external DNS, Pi-hole, or real provider APIs.

## Acceptance criteria

- Metrics expose outbound client lifecycle and DNS cache behavior.
- Dashboard or API exposes a compact sanitized network diagnostics summary.
- DNS cache hit/miss/resolution/error/stale behavior can be observed without packet capture.
- Outbound client construction can be observed and does not scale with request count.
- Documentation explains defaults, disablement, and Pi-hole/SBC validation.
- Manual validation procedure is documented and reproducible.
- No diagnostic path exposes API keys, auth headers, prompts, completions, request bodies, or full URLs.
- CI covers diagnostics redaction and metric/reporting behavior with mocks.

## Risks and mitigations

Risk: Diagnostics could leak provider secrets.

Mitigation: Use explicit DTOs for diagnostics instead of dumping request/client structs. Add tests for redaction and absence of sensitive fields.

Risk: Excessive debug logging could increase SD-card writes.

Mitigation: Keep normal logs quiet. Cache-hit logging should be trace-only or omitted entirely.

Risk: Dashboard scope creep.

Mitigation: Add a compact status panel first. Detailed host tables should be optional/expandable.

Risk: Pi-hole validation is environment-specific.

Mitigation: Treat Pi-hole validation as manual documentation, not a CI requirement.

Risk: Host labels could become high-cardinality if arbitrary provider URLs are configured.

Mitigation: Keep labels sanitized and consider provider-level aggregation if arbitrary hosts become common.

## Final closure checklist

- Outbound clients are centralized and reused.
- DNS cache is bounded, TTL-based, and disableable.
- TLS hostname/SNI behavior is preserved.
- Metrics and diagnostics are present.
- Dashboard/API/CLI surfaces are sanitized.
- Tests cover resolver behavior and diagnostics behavior.
- SBC/Pi-hole manual validation notes are documented.
- Defaults are conservative and suitable for Raspberry Pi-class deployments.

## Handoff notes

This phase should be implemented after the outbound client lifecycle cleanup and bounded DNS cache. Keep the diagnostics intentionally small. The purpose is to prove the network optimization works and remains safe, not to introduce a full observability product inside eggpool.
