# Configuration Profiles

Profiles are TOML starting points for ordinary hosts and SBCs. The native
runtime validates all profiles before startup or rehash.

## Lean default

Use `config.sbc.example.toml` as the complete low-wear example. It keeps
diagnostics, external metadata work, and automatic backups opt-in while
retaining durable request/accounting writes and SQLite WAL.

## General guidance

`[server].threads` is accepted for compatibility and reported in runtime
diagnostics; the current Rust candidate remains single-threaded at the server
execution boundary. Request concurrency is controlled by the native runtime,
connection-pool limits, provider backpressure, and bounded database work.

`[database].worker_threads` controls database worker capacity. Increase it only
after measuring contention on the target host. Metrics and trace settings trade
dashboard freshness and detail against storage I/O; backup and model-info
refresh are explicit operational choices.

Always validate a profile before use:

```bash
eggpool --config config.toml check-config
```
