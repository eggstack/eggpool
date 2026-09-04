# Provider Transport Planning Baseline Evidence

Status: planning evidence; not an implementation contract

Repository baseline: `13a6a557a07a41a5df5c5f044c8282c7ce8edf73`

This note records the evidence used to scope the M4 Provider HTTP + Eggress workstream. T001 owns the authoritative executable contract and may refine these findings after running the Python/Eggress fixture corpus.

## Python boundary observed

- `ProviderClientPool` owns long-lived provider clients and account-specific clients.
- Only accounts with a resolved proxy receive a dedicated account client; direct accounts fall back to the provider client.
- Provider HTTPX clients use provider base URL plus connect/read/write/pool timeouts, connection/keepalive limits, and keepalive expiry.
- The Python pproxy adapter contributes a TCP stream beneath httpcore/HTTPX. HTTP/TLS/pooling remain above that stream.
- `OutboundClientManager` is a separate non-provider/background-network abstraction and is not part of M4.

## Eggress boundary observed

Eggress 1.0.2 `eggress-embed` exposes `OutboundConnector::from_pproxy_uri` and `connect_tcp` without starting a listener. The crate defaults to a broad `full` feature set, so EggPool must use `default-features = false` and enable only the protocols proven necessary by T001.

The maintained Eggress pproxy matrix has strong coverage for HTTP/SOCKS and broader supported-difference coverage for encrypted/legacy/SSH forms, but Eggress-wide parity is not accepted as EggPool parity. The EggPool proxy docs currently claim a broad pproxy URI surface, so the exact working Python subset must be probed before Rust feature selection.

## Initial architecture conclusion

M4 is a transport workstream, not an inference workstream. Use one Hyper/hyper-util/Rustls HTTP stack for both direct and proxied provider clients; inject Eggress only at TCP establishment for proxied accounts. Keep routing, provider codecs/auth, retries, finalization, catalog policy, and runtime generations out of the workstream.