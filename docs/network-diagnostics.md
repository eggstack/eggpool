# Network diagnostics

EggPool uses HTTPX connection pooling for upstream requests. Ordinary host
name resolution is delegated to the operating system and reused through the
connection pool; EggPool no longer maintains a process-local DNS cache.

`GET /api/network/diagnostics` reports bounded outbound-client and provider
client-pool counters. It does not expose resolver caches, host entries, or
credential material.

The network section of `eggpool runtime-status` shows the same outbound and
provider-pool information. For DNS troubleshooting, use the host operating
system's resolver tools and inspect the provider connectivity errors recorded
by EggPool. Per-account proxy routing remains supported through the configured
proxy transport.
