# Network diagnostics

EggPool uses its native Eggfetch provider clients for upstream requests. Ordinary
host name resolution is delegated to the operating system and reused through
the connection pool; EggPool does not maintain a process-local DNS cache.

There is no `/api/network/diagnostics` HTTP endpoint. The network section of
`eggpool runtime-status` reports the bounded outbound-client and provider-pool
counters; it does not expose resolver caches, host entries, or credential
material. For DNS troubleshooting, use the host operating system's resolver
tools and inspect the provider connectivity errors recorded by EggPool.
Per-account proxy routing remains supported through the configured proxy
transport.
