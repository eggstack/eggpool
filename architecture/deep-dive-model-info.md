# Deep Dive: Model-Info Enrichment

Back to [Architecture](README.md)

See also the review index in [overview.md](overview.md) (§8).

## Ownership

Model-info enrichment is bounded catalog-attached work, not a standalone subsystem.
Authority is `rust/src/config.rs::ModelInfoConfig` (plus `ModelInfoSourcesConfig`,
`ModelInfoAliasConfig`, `ModelInfoOverrideConfig`), `rust/src/operations/operator.rs`
(canonical/alias reads and repairs), `rust/src/runtime.rs::modelinfo` (CLI adapter),
`rust/src/cli.rs::ModelInfoCommand`, `rust/src/task_supervisor.rs`
(`catalog_refresh` inventory), and the `model_info_canonical` / `model_info_aliases`
tables. There is no model-info scheduler: startup may run one bounded external pass when
`startup_refresh` is set, and later work rides the generation-leased `catalog_refresh`
task (`TaskOwnership::ActiveGenerationLeased`).

## Configuration

`ModelInfoConfig` bounds the work: `enabled`, `startup_refresh`, `refresh_interval_s`,
`known_ttl_s`, `partial_ttl_s`, `sparse_new_initial_ttl_s`, `sparse_new_later_ttl_s`,
`sparse_new_accelerated_days`, `conflict_ttl_s`, `max_models_per_cycle`,
`include_in_models_endpoint`, `store_raw_observations`, `sources`, `aliases`, and
`overrides`. `ModelInfoSourcesConfig` declares `provider_catalog`, `openrouter`,
`artificial_analysis`, and `huggingface` via `ModelInfoSourceConfig` (`enabled`,
`priority`, `ttl_seconds`, `base_url`, `api_key_env`, `max_entries`); defaults keep the
provider catalog highest priority and artificial-analysis/huggingface disabled.
`ModelInfoAliasConfig` maps (`provider_id`, `model_id`, `source`, `source_model_id`,
`confidence`, `notes`); `ModelInfoOverrideConfig` carries operator display/status pins
(`summary`, `family`, `display_name`, `notes`, `hide_benchmark_sources`,
`status_override`).

## Enrichment and provenance

The provider catalog remains the authoritative discovery source (`refresh.rs`). Bounded
`openrouter`/`artificial_analysis`/`huggingface` passes may fill capability dimensions the
provider catalog omits (limits, modalities, thinking signals) with per-source TTL,
cooldown, and next-refresh state. Verified metadata never invents provider support, and
explicit operator overrides win over every external source. External failures are isolated:
they degrade enrichment only and never block discovery, eligibility, or routing.
`EvidenceProvenance::ModelInfo` marks this evidence distinctly from `RuntimeHttp`,
`ProviderCatalog`, `ManualOverride`, `OperatorAction`, and `MigrationLegacy`.

## Operator paths

`operations/operator.rs` exposes `list_model_info`, `show_model_info`,
`refresh_model_info_from_catalog`, `repair_model_info`, `list_aliases`,
`seed_configured_aliases`, and `validate_model_info_status`. The CLI
(`eggpool modelinfo aliases|list|show|refresh|repair` via `cli.rs::ModelInfoCommand` and
`runtime.rs::modelinfo`) is the only mutation path: `refresh` rebuilds canonical rows from
the catalog plus configured aliases, `repair` backfills bounded limit facts over a capped
scan, and `list`/`show`/`aliases` are read-only bounded selects. No credentials, prompts,
raw bodies, or cache keys enter these rows or diagnostics.

## Invariants

- No standalone scheduler; enrichment rides startup plus the leased `catalog_refresh` tick.
- Provider catalog stays authoritative; external sources only fill omissions.
- Operator overrides are final; external data cannot reverse them.
- External failures never fail discovery or routing.
- Operator reads are bounded and secret-free; unknown capability stays distinct from
  unsupported.

## Verification

```bash
cargo test --manifest-path rust/Cargo.toml --test catalog_refresh -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o002 -- --test-threads=1
```
