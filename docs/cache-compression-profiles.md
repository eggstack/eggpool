# Cache and compression profiles

These profiles use only the supported runtime surface. They can be merged into
the shipped `config.example.toml`; unknown removed sections are rejected by
configuration validation.

## Default: reporting only

```toml
[compression]
enabled = false
mode = "observe"
placement = "suffix_only"
respect_cache_boundaries = true
```

## Observe opportunities

```toml
[compression]
enabled = true
mode = "observe"
placement = "suffix_only"
min_candidate_tokens = 2048
min_savings_tokens = 1024
max_compression_latency_ms = 25.0
```

## Safe suffix transforms

```toml
[compression]
enabled = true
mode = "safe"
placement = "suffix_only"
respect_cache_boundaries = true
```

Safe mode fails closed if its stable-prefix integrity check changes. Native
provider cache boundaries remain contract-controlled and are not synthesized by
EggPool.
