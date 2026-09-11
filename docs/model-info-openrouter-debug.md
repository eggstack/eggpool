# Model-Info OpenRouter Diagnostics

Model-info enrichment is owned by the native Rust catalog lifecycle. When
`[model_info].enabled = true`, startup may perform one bounded enrichment pass;
later opportunities come from `catalog_refresh` and select only due rows.

## Safe diagnostics

Use the native CLI against a disposable configuration when investigating
enrichment:

```bash
eggpool --config config.toml check-config
eggpool --config config.toml modelinfo refresh
eggpool --config config.toml modelinfo list
eggpool --config config.toml runtime-status --json
```

The manual refresh is a diagnostic/recovery operation, not a requirement for
ordinary catalog discovery. External failures are isolated from provider model
availability and routing. Keep API keys in the environment or adjacent `.env`
and do not paste raw provider responses into issue records.
