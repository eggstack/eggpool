# Persistence Control/Projection Investigation (M010)

Status: investigation complete; split rejected for current history/availability contract

This is a bounded architecture record, not a runtime design decision. Production remains the schema-54 monolithic SQLite database on the single `Database` connection/gate/worker with WAL/NORMAL. M007 and M008 physical qualification evidence remains in their immutable closure records. This investigation found a real potential reduction in foreground index fanout, but no bounded, lossless backlog and backup contract compatible with current request-history visibility and continued admission through arbitrarily long analytics outages. No split-storage ADR is proposed.

## 1. Evidence and authority

- `rust/assets/db/migrations/0001_initial.sql` through `0054_model_quarantine_null_identity.sql` define the current schema; `checksums.json` pins the immutable migration corpus.
- `rust/src/coordinator/publication.rs` atomically creates/updates the request, active reservation, attempt, and `routing_decisions` row. `rust/src/coordinator/finalization.rs` terminalizes request/attempt and releases reservation in one transaction.
- `rust/src/coordinator/reconciliation.rs` treats pending requests, open attempts, and active reservations as crash-recovery evidence.
- `rust/src/db/repositories.rs` owns request, attempt, dashboard, usage-rollup, ping, event, catalog, quarantine, pricing, and retention queries. `rust/src/operations/backup.rs` owns version-1 single-database snapshots and restore staging.
- `plans/closure/persistence/007-pi5-qualification.md` rejects the dedicated PASSIVE writer because it failed WAL progress/convergence. `plans/closure/persistence/008-status.md` rejects PERSIST/EXTRA on request-p95 and worker-write-proxy gates. Those measurements do not identify table/index costs or physical NAND amplification.

## 2. Current critical-path write graph

```text
publication transaction
  request identity/pending state ─┐
  reservation ownership          ├─ same SQLite transaction / one gate
  attempt identity/open state    ┤
  routing decision trace         ┘

finalization transaction
  request terminal + usage facts ─┐
  attempt terminal + outcome      ├─ same SQLite transaction / one gate
  reservation release            ┘
```

Retries update request account/reservation association and append the next attempt only after the previous attempt and reservation satisfy the durable release boundary. Recovery repairs only those durable control rows. `routing_decisions` is observational but is deliberately committed beside the attempt so the trace cannot disagree with the selected attempt. Finalization's request row is mixed: status, identity, and terminal transition are control; token/cost/cache/latency/compression/transcoding/error facts feed history, dashboard, rollups, and trace views. `request_attempts` is also mixed: attempt number, ownership, open/terminal state, and retry ordering are control; detailed result facts serve history and trace consumers.

## 3. Table, field, index, and consumer classification

The following inventory is generated from the ordered schema-54 migration SQL using SQLite `sqlite_master`, `PRAGMA table_info`, and `PRAGMA index_list`. Index names shown are the extant indexes, including SQLite primary/unique autoindexes where present. The `requests` field list was inspected at schema 54; its fields are divided by consumer class below rather than treating the whole table as analytics. “Control” fields are needed on the foreground/recovery/routing path; “projection” fields can be emitted after durable control transition only if equivalent history reads and retention are preserved.

| Table | Class and field/consumer split | Maintained indexes (schema 54) |
|---|---|---|
| `accounts` | Control-authoritative: identity, enablement, weight, provider relation; routing/catalog hydration and configuration mutations. `api_key_env` is a secret reference and must never enter projection payloads. | `idx_accounts_enabled`, unique autoindex |
| `providers` | Control-authoritative provider identity, base URL and enablement/protocol metadata; routing/catalog hydration. Never copy URL/config to analytics events. | unique autoindex |
| `models` | Control-authoritative model identity/protocol/capabilities/endpoint resolution for admission/catalog/routing. Descriptive fields are read by APIs but remain catalog authority. | unique autoindex |
| `account_models` | Control-authoritative account/model eligibility; routing. | `idx_account_models_model`, `idx_account_models_account`, PK autoindex |
| `provider_model_metadata` | Control/catalog metadata used to build provider model views; refresh and integration reads. Could be separately regenerated only with a proven catalog rebuild contract. | `idx_provider_model_metadata_provider`, unique autoindex |
| `catalog_refresh_state` | Control/operational cursor: last success/outcome/count drives refresh decisions and readiness diagnostics. | `idx_catalog_refresh_state_provider` |
| `requests` | **Mixed.** Control: `id`, `proxy_request_id`, `status`, `account_id`, `provider_id`, `model_id`, `protocol`, `streamed`, `reserved_microdollars`, `started_at`, `first_attempt_at`, `last_attempt_id`, and retry identity. These support idempotency, routing association, retry, finalization conflict detection, and recovery. Projection: terminal timestamps and bounded response facts (usage, costs, cache counters, latency phases, byte counts, error classification/detail, upstream request identity, protocol outcome, and compression/transcoding/segmentation/cache observations). Readers include coordinator publication/finalization/recovery, quota hydration, DashboardRepository, statistics, trace/detail routes, retention, backup/restore and repair tooling. Finalization transition cannot become eventual. | `idx_requests_account`, `idx_requests_model`, `idx_requests_started`, `idx_requests_status`, `idx_requests_proxy_request_id` (unique), `idx_requests_account_started`, `idx_requests_streamed_provider_model_started_ttft`, `idx_requests_client_ip_started`, `idx_requests_streamed_started_ttft`, `idx_requests_original_model_id`, `idx_requests_ttfb_ms`, `idx_requests_cache_counter_status`, `idx_requests_transcoded`, compression status/mode/applied indexes, segmentation status, synthetic-cache status/policy indexes, exactness/completed/protocol indexes, and `idx_requests_status_started` |
| `reservations` | Control-authoritative throughout: active/released/expired state, request/account/model/original-model relation, expiry and release reason; quota, compensation, recovery, retention. | account, model, status, request, expires, status+expires, original-model indexes |
| `request_attempts` | **Mixed.** Control: row id, request id, attempt number, account id, started/completed/open state, retry outcome needed to enforce no next attempt before durable prior release. Projection: response status, sanitized error facts, upstream id, byte counts, provider/model/protocol, latency, streamed/retry diagnostics. Used by publication, finalization, recovery, trace/dashboard, retention and backup. | request, account, account_id, provider+started, model+started, status+started, retry-category+started indexes |
| `routing_decisions` | Projection candidate, but emitted with an outbox record atomically with attempt creation to preserve the attempt/decision correspondence. Dashboard/trace readers and retention consume it; no routing selector reads it. | request, model+time, provider+time, selected-account+time, retention indexes |
| `usage_rollups` | Derived aggregate. Built from finalized usage, read by stats/timeseries, retention-limited; regenerable only from retained request facts or a snapshot. | composite PK autoindex, bucket, provider+model+bucket, account+bucket indexes |
| `provider_pings` | Independent operational history; probe and dashboard freshness, no inference correctness dependency. | provider, probed-at, provider+probed-at indexes |
| `operational_events` | Independent bounded operational history for recovery/cleanup observability; dashboard/runtime stats read it. Not recovery authority. | type+occurred, occurred indexes |
| `account_events` | Independent account/operator event history; dashboard reads it. | account, type, created indexes |
| `account_backoffs` | Control-authoritative routing eligibility/backoff, including error/status facts and expiry. | unique identity, active, account+model indexes |
| `model_quarantine` | Control-authoritative model eligibility/quarantine/expiry and evidence classification; selector reads it. Error class/status and bounded reason remain policy evidence. | unique identity (including null-upstream unique index), state, expiry, model indexes |
| `model_price_snapshots` | Mixed catalog/pricing history. Current eligible price is consulted by cost projection and quota estimates; old snapshots are history. Cannot move current selection inputs without a snapshot/refresh contract. | model, captured-at, model+provider+captured, model+source indexes |
| `model_pricing_aliases` | Control-authoritative provider/catalog model price mapping; catalog/price resolution. | catalog, provider, unique identity indexes |
| `request_cost_repairs` | Independent maintenance/audit history of cost corrections; not request admission or recovery authority. | request, repaired-at indexes |
| `transcoding_daily` | Derived aggregate for dashboard compatibility counters; reconstructible only from an agreed retained request projection. | PK autoindex |
| `health_probe` | Independent legacy health probe scratch/state; no request-history consumer. | none |
| `model_info_canonical` | Catalog-facing derived metadata refreshed from sources and read by model-info APIs; safe regeneration needs external refresh availability and is not request-history projection. | unique identity, status, next-refresh indexes |
| `model_info_observations` | Independent source observation cache/history, expiry and model-info reads. `raw_json` is source metadata, not request/provider bodies; do not copy into request events. | unique identity, model+source, source+model indexes |
| `model_info_aliases` | Catalog/model-identity support for model-info reads and matching; metadata refresh dependency. | unique identity, alias indexes |
| `model_info_source_health` | Independent source-refresh scheduling/backoff and diagnostic state. | source PK autoindex |
| `model_info_overrides` | Control/operator-authored model-info override state; user mutation must stay durable before success. | model PK autoindex |
| `model_info_match_evidence` | Derived matching evidence/history, model-info diagnostics; not request correctness. | model, source indexes |
| `compression_tuning_recommendations` | Control/policy workflow state: pending/accepted recommendation lifecycle can influence applied compression policy and requires durable acknowledgement. | status, policy PK autoindex |
| `compression_tuning_overrides` | Control/policy workflow state with expiry; affects request transform behavior. | policy index |

The column names underlying the field-group classifications above are included here so the inventory is reproducible and no table's mixed ownership is inferred from its name:

- `accounts`: `id`, `name`, `api_key_env`, `enabled`, `weight`, `created_at`, `provider_id`.
- `providers`: `id`, `provider_id`, `base_url`, `protocols`, `enabled`, `created_at`.
- `models`: `model_id`, `display_name`, `protocol`, `capabilities`, `source_metadata`, `first_seen_at`, `last_seen_at`, `protocol_source`, `endpoint_path`, `resolution_status`, `provider_id`.
- `account_models`: `account_id`, `model_id`, `enabled`, `created_at`.
- `provider_model_metadata`: `model_id`, `provider_id`, `display_name`, `protocol`, `capabilities`, `source_metadata`, `protocol_source`, `first_seen_at`, `last_seen_at`, `resolution_status`.
- `catalog_refresh_state`: `account_id`, `provider_id`, `last_successful_refresh_at`, `last_outcome`, `model_count`.
- `requests` (100 columns): `id`, `account_id`, `model_id`, `started_at`, `completed_at`, `status`, `input_tokens`, `output_tokens`, `cost_microdollars`, `upstream_latency_ms`, `error_message`, `protocol`, `streamed`, `exactness`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`, `thinking_characters`, `reserved_microdollars`, `first_byte_ms`, `retry_count`, `upstream_request_id`, `error_class`, `error_detail`, `status_code`, `proxy_request_id`, `bytes_received`, `bytes_emitted`, `provider_id`, `client_ip`, `original_model_id`, `first_attempt_at`, `last_attempt_id`, `upstream_connect_ms`, `upstream_read_ms`, `coordinator_overhead_ms`, `provider_cost_microdollars`, `provider_cost_source`, `local_cost_microdollars`, `local_cost_exactness`, `upstream_protocol`, `thinking_trace_json`, `cache_counter_status`, `cached_input_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `cache_write_input_tokens`, `cache_write_input_reported`, `input_tokens_reported`, `output_tokens_reported`, `total_tokens_reported`, `request_shape_hash`, `stable_prefix_hash`, `transcoded`, `raw_usage_json`, `segmentation_status`, `stable_prefix_estimated_tokens`, `semi_stable_estimated_tokens`, `volatile_estimated_tokens`, `stable_prefix_bytes`, `semi_stable_bytes`, `volatile_bytes`, `segmentation_summary_json`, `compression_status`, `compression_mode`, `compression_candidate_count`, `compression_eligible_candidate_count`, `compression_suppressed_candidate_count`, `compression_estimated_original_tokens`, `compression_estimated_compressed_tokens`, `compression_estimated_savings_tokens`, `compression_analyzer_latency_ms`, `compression_warning_count`, `compression_reason_code_counts_json`, `compression_summary_json`, `compression_applied`, `compression_transform_count`, `compression_transforms_by_reason_json`, `compression_original_tokens`, `compression_compressed_tokens`, `compression_savings_tokens`, `compression_pre_stable_prefix_hash`, `compression_post_stable_prefix_hash`, `compression_stable_prefix_preserved`, `compression_warnings_json`, `compression_latency_ms`, `compression_failed_fallback`, `compression_applied_summary_json`, `compression_policy_name`, `compression_policy_source`, `compression_policy_warnings_json`, `synthetic_cache_status`, `synthetic_cache_dry_run`, `synthetic_cache_candidate_count`, `synthetic_cache_applied_count`, `synthetic_cache_warning_count`, `synthetic_cache_warnings_json`, `synthetic_cache_policy_name`, `synthetic_cache_policy_source`, `synthetic_cache_summary_json`.
- `reservations`: `id`, `request_id`, `account_id`, `model_id`, `reserved_microdollars`, `created_at`, `released_at`, `status`, `estimated_tokens`, `expires_at`, `release_reason`, `original_model_id`.
- `request_attempts`: `id`, `request_id`, `attempt_number`, `account_id`, `started_at`, `completed_at`, `status_code`, `error_class`, `upstream_request_id`, `bytes_emitted`, `error_detail`, `provider_id`, `model_id`, `protocol`, `retry_category`, `release_reason`, `bytes_received`, `latency_ms`, `streamed`, `is_retry_outcome`.
- `routing_decisions`: `id`, `request_id`, `attempt_number`, `model_id`, `provider_id`, `protocol`, `selected_account_id`, `selected_account_name`, `selected_tier`, `selected_score`, `eligible_count`, `scored_count`, `attempted_excluded_count`, `top_score`, `top_score_account_name`, `exclude_reasons_json`, `decision_made_at`, `score_components_json`.
- `usage_rollups`: `bucket_start`, `bucket_size_s`, `provider_id`, `model_id`, `account_id`, `protocol`, `streamed`, `status`, `request_count`, `error_count`, `retry_count`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens`, `thinking_characters`, `cost_microdollars`, `bytes_received`, `bytes_emitted`, `latency_ms_sum`, `latency_ms_min`, `latency_ms_max`, `first_byte_ms_sum`, `first_byte_ms_count`, `created_at`, `updated_at`.
- `provider_pings`: `id`, `provider_id`, `account_name`, `probed_at`, `latency_ms`, `status_code`, `error`, `model_count`.
- `operational_events`: `id`, `event_type`, `details_json`, `occurred_at`.
- `account_events`: `id`, `account_id`, `event_type`, `details`, `created_at`.
- `account_backoffs`: `id`, `account_id`, `model_id`, `reason`, `status_code`, `error_class`, `consecutive_failures`, `backoff_until`, `last_failure_at`, `updated_at`.
- `model_quarantine`: `id`, `provider_id`, `account_id`, `canonical_model_id`, `upstream_model_id`, `upstream_protocol`, `state`, `evidence_provenance`, `reason`, `first_observed`, `last_observed`, `observation_count`, `expiry`, `cleared_at`, `clear_reason`, `last_status_code`, `last_error_class`, `updated_at`.
- `model_price_snapshots`: `id`, `model_id`, `input_price_per_1k`, `output_price_per_1k`, `captured_at`, `input_per_million_microdollars`, `output_per_million_microdollars`, `source`, `metadata_json`, `cache_read_per_million_microdollars`, `cache_write_per_million_microdollars`, `provider_id`, `source_detail`, `source_confidence`, `catalog_source`.
- `model_pricing_aliases`: `provider_id`, `upstream_model_id`, `catalog_source`, `catalog_model_id`, `confidence`, `notes`, `created_at`, `updated_at`.
- `request_cost_repairs`: `id`, `request_id`, `old_cost_microdollars`, `new_cost_microdollars`, `old_exactness`, `new_exactness`, `reason`, `provider_filter`, `since_date`, `repaired_at`.
- `transcoding_daily`: `day`, `client_protocol`, `upstream_protocol`, `request_count`, `loss_warning_count`.
- `health_probe`: `id`, `probe_at`.
- `model_info_canonical`: `model_id`, `status`, `summary`, `detail_json`, `provenance_json`, `conflicts_json`, `sparse`, `first_seen_at`, `last_seen_at`, `last_refreshed_at`, `next_refresh_at`.
- `model_info_observations`: `id`, `model_id`, `provider_id`, `source`, `source_model_id`, `observed_at`, `expires_at`, `confidence`, `raw_hash`, `normalized_json`, `raw_json`.
- `model_info_aliases`: `model_id`, `provider_id`, `alias`, `source`, `confidence`, `active`, `first_seen_at`, `last_seen_at`, `notes`, `match_method`, `discovered_by`, `diagnostics_json`.
- `model_info_source_health`: `source`, `enabled`, `last_success_at`, `last_error_at`, `last_error_class`, `last_error_message`, `cooldown_until`, `failure_count`, `last_status_code`, `rate_limited_until`, `last_success_duration_ms`, `last_payload_count`.
- `model_info_overrides`: `model_id`, `summary`, `family`, `display_name`, `notes`, `hide_benchmark_sources`, `status_override`, `created_at`, `updated_at`.
- `model_info_match_evidence`: `id`, `model_id`, `provider_id`, `source`, `alias`, `match_method`, `confidence`, `diagnostics_json`, `created_at`, `last_seen_at`.
- `compression_tuning_recommendations`: `policy_name`, `status`, `recommendation_json`, `generated_at`, `created_at`.
- `compression_tuning_overrides`: `id`, `policy_name`, `fields_json`, `reason_codes_json`, `generated_at`, `expires_at`.

### Complete schema-54 index census

This census records every `PRAGMA index_list` result, including autoindexes for primary/unique constraints. It is separate from the maintained-index column above, which highlights the request/frequent-write indexes relevant to write fanout.

- `account_backoffs`: `idx_account_backoffs_account_model`, `idx_account_backoffs_active`, `sqlite_autoindex_account_backoffs_1`.
- `account_events`: `idx_account_events_created`, `idx_account_events_type`, `idx_account_events_account`.
- `account_models`: `idx_account_models_model`, `idx_account_models_account`, `sqlite_autoindex_account_models_1`.
- `accounts`: `idx_accounts_enabled`, `sqlite_autoindex_accounts_1`.
- `catalog_refresh_state`: `idx_catalog_refresh_state_provider`.
- `compression_tuning_overrides`: `idx_compression_tuning_overrides_policy`.
- `compression_tuning_recommendations`: `idx_compression_tuning_recommendations_status`, `sqlite_autoindex_compression_tuning_recommendations_1`.
- `health_probe`: none.
- `model_info_aliases`: `idx_model_info_aliases_alias`, `sqlite_autoindex_model_info_aliases_1`.
- `model_info_canonical`: `idx_model_info_canonical_next_refresh`, `idx_model_info_canonical_status`, `sqlite_autoindex_model_info_canonical_1`.
- `model_info_match_evidence`: `idx_model_info_match_evidence_source`, `idx_model_info_match_evidence_model`.
- `model_info_observations`: `idx_model_info_observations_source_model`, `idx_model_info_observations_model_source`, `sqlite_autoindex_model_info_observations_1`.
- `model_info_overrides`: `sqlite_autoindex_model_info_overrides_1`.
- `model_info_source_health`: `sqlite_autoindex_model_info_source_health_1`.
- `model_price_snapshots`: `idx_price_snapshots_model_provider_captured`, `idx_price_snapshots_model_source`, `idx_model_price_snapshots_captured`, `idx_model_price_snapshots_model`.
- `model_pricing_aliases`: `idx_pricing_aliases_provider`, `idx_pricing_aliases_catalog`, `sqlite_autoindex_model_pricing_aliases_1`.
- `model_quarantine`: `uq_model_quarantine_null_upstream`, `idx_model_quarantine_model`, `idx_model_quarantine_expiry`, `idx_model_quarantine_state`, `sqlite_autoindex_model_quarantine_1`.
- `models`: `sqlite_autoindex_models_1`.
- `operational_events`: `idx_operational_events_occurred`, `idx_operational_events_type_occurred`.
- `provider_model_metadata`: `idx_provider_model_metadata_provider`, `sqlite_autoindex_provider_model_metadata_1`.
- `provider_pings`: `idx_provider_pings_provider_probed`, `idx_provider_pings_probed`, `idx_provider_pings_provider`.
- `providers`: `sqlite_autoindex_providers_1`.
- `request_attempts`: `idx_request_attempts_retry_category_started`, `idx_request_attempts_model_started`, `idx_request_attempts_provider_started`, `idx_request_attempts_account`, `idx_request_attempts_request`.
- `request_cost_repairs`: `idx_request_cost_repairs_repaired_at`, `idx_request_cost_repairs_request`.
- `requests`: `idx_requests_synthetic_cache_policy_name`, `idx_requests_synthetic_cache_status`, `idx_requests_compression_policy_name`, `idx_requests_compression_applied`, `idx_requests_compression_mode`, `idx_requests_compression_status`, `idx_requests_segmentation_status`, `idx_requests_transcoded`, `idx_requests_cache_counter_status`, `idx_requests_ttfb_ms`, `idx_requests_original_model_id`, `idx_requests_streamed_started_ttft`, `idx_requests_client_ip_started`, `idx_requests_streamed_provider_model_started_ttft`, `idx_requests_account_started`, `idx_requests_proxy_request_id`, `idx_requests_status_started`, `idx_requests_protocol`, `idx_requests_completed`, `idx_requests_exactness`, `idx_requests_started`, `idx_requests_model`, `idx_requests_account`.
- `reservations`: `idx_reservations_original_model_id`, `idx_reservations_status_expires`, `idx_reservations_expires`, `idx_reservations_request`, `idx_reservations_status`, `idx_reservations_model`, `idx_reservations_account`.
- `routing_decisions`: `idx_routing_decisions_retention`, `idx_routing_decisions_selected_account`, `idx_routing_decisions_provider_started`, `idx_routing_decisions_model_started`, `idx_routing_decisions_request`.
- `transcoding_daily`: `sqlite_autoindex_transcoding_daily_1`.
- `usage_rollups`: `idx_usage_rollups_account`, `idx_usage_rollups_provider_model`, `idx_usage_rollups_bucket`, `sqlite_autoindex_usage_rollups_1`.

### Critical mixed-field boundaries

The minimum request authority is request key/id, ingress idempotency key, pending/terminal state, model/protocol/stream identity, routing/account/provider association, admission reservation amount, start/first-attempt/last-attempt ordering, and the terminal transition result used for idempotency/recovery. Terminal usage and bounded diagnostics are projection facts only after that authority transition commits. Since a repeated terminal callback compares status/identity and existing dashboard APIs read the resulting usage immediately, removing synchronous detailed fields changes read freshness and repair semantics even if it does not change routing.

The minimum attempt authority is request/attempt identity, attempt number, account ownership, start/open/terminal state, and the terminal outcome needed to gate retries/recovery. Detailed byte/latency/error/protocol facts are projection candidates subject to existing bounded sanitization. Active reservation ownership, amount, expiry, release and relationship remain control-authoritative in full.

## 4. Options and critical-path shape

| Option | Foreground transaction | Structural effect | Operational effect | Finding |
|---|---|---|---|---|
| A. Monolithic authority | Existing request + reservation + attempt + routing decision on publish; request + attempt + reservation on terminalization | Full current index fanout and synchronous history visibility | One-file backup/restore and one retention authority | Safest current contract; remains production |
| B. Same-file logical/field split | Correctness rows plus bounded projection/outbox row still in same SQLite file and writer | May remove indexes from control tables only if old history table writes/indexes are not also maintained. If projection rows continue in same DB, total page/WAL writes remain; routing decision atomicity still costs a write. | One backup, but foreground still shares same WAL/gate; compatibility views/migration add complexity | Plausible small index-pruning follow-up only after consumer evidence; does not address isolation; no decision to implement |
| C. Control + outbox + analytics file | Control mutation and one outbox append commit atomically; analytics apply happens later in its own transaction | Potentially fewer request-path secondary indexes; analytics writes leave control checkpoint domain. M010 prototype observes fewer modeled control indexes, but adds one outbox row for each publish/finalize and duplicates retained history while lagged. | New process-owned projector, backlog policy, two-file backup/restore, lagged dashboard reads, schema compatibility, corruption recovery | Structurally promising, operational contract not viable under the current requirements; split rejected for now |

Synchronous dual writes, `ATTACH`, distributed transactions, WAL2/forks, and lossy in-memory queues are rejected. The test-only model has 15 explicit baseline indexes versus 2 control indexes; this is a synthetic schema subset, not a count of all indexes touched in the production transaction and not a performance ratio. Candidate outbox event count is 2/request (publication and terminalization). Analytics apply is measured in a separate file. Local file/WAL deltas are printed by the test but are not Pi/MMC latency, SSD/NAND write amplification, or production qualification.

## 5. Proposed event/projector contract (not adopted)

The narrowest lossless contract would use a signed-range monotonic `INTEGER event_id` allocated by the control SQLite writer, `schema_version`, closed `event_kind`, request id, attempt number, and a bounded canonical JSON payload. Payloads copy only facts needed to reproduce current history/trace fields: selected route facts, terminal status, allowlisted token/cost/cache/latency/byte facts, bounded sanitized error classification/detail and protocol outcome. It references no mutable control row because cleanup, account rename, or retention must not alter already-committed history. It excludes prompts, request bodies, credentials, raw provider bodies, filesystem paths, and cache keys. A strict encoded payload ceiling (proposed 16 KiB/event) is required; overflow fails the encompassing correctness transaction instead of silently truncating parity facts.

One process-owned projector reads ordered `event_id` batches with a fixed batch limit. Analytics applies projection rows and advances a singleton applied cursor in the same `BEGIN IMMEDIATE` transaction. The event id is the idempotency key; duplicate delivery is a no-op. The cursor is contiguous: an unsupported schema/kind or poison record does not advance past it. Retry uses bounded exponential backoff with jitter and a supervised shutdown token; cancellation before analytics commit is rolled back, cancellation after commit sees the durable cursor. Outbox deletion is permitted only at or below the durable analytics cursor and only when the snapshot/retention protocol no longer needs the event. Counters are scalar counts/bytes/age/retry class only.

The event contract itself does not solve boundedness. With an unavailable analytics file, a lossless outbox grows until it reaches any finite hard byte cap. At that point the choices are (1) block/reject otherwise-valid inference/control transitions, (2) drop required request history, (3) accept unbounded disk use, or (4) reduce or expire current public history semantics. M010 has no authority/evidence to select options 1, 2, or 4, while option 3 violates bounded-resource invariants. Hence Option C is not viable under the current contract. A soft threshold may alert and slow projection retries but does not change this proof.

| Condition | Correctness-safe handling in candidate | Contract blocker |
|---|---|---|
| Analytics unavailable minutes to days | Continue control commits; retain outbox and retry | Finite disk budget eventually forces one of the prohibited choices above |
| Poison event / unsupported schema | Stop at event; preserve cursor; expose bounded failure class | Requires compatible deploy/repair tooling; cannot skip without history gap |
| Disk near hard limit / backlog above hard cap | Current proposal has no safe automatic action | Blocking violates continued admission; dropping violates parity; unbounded growth violates resource bound |
| Analytics DB corrupt | Control remains recoverable and can keep emitting only until bounded backlog limit | Restore/rebuild requires retained outbox or a verified snapshot; neither is guaranteed at arbitrary outage age |
| Retention races projection | Reclaimer may delete only through applied cursor and snapshot-safe watermark | Stuck cursor pins outbox bytes, creating same cap failure |

## 6. Backup, restore, and read consistency

Rebuild-from-control alone would require retaining every event needed to reconstruct all publicly visible request/attempt history. That is indefinite event retention and duplicates the current history storage; it does not satisfy a finite disk cap. Deleting history under existing retention is only safe if the product's API retention contract is formalized and projection cleanup is equivalent; M010 does not introduce that contract.

Coordinated snapshots would pause the projector and reclaimer at a durable applied cursor, take an online analytics snapshot, then take a control snapshot while retaining every outbox event after the cursor. Traffic could continue only if the control snapshot and event tail remain ordered at the captured point; otherwise request admission must be quiesced. Archive version must name both database snapshots, each schema version/checksum, the captured analytics cursor, control's maximum event id, and retained tail. Restore validates both snapshots and `0 <= analytics_cursor <= control_max_event_id`, opens control first, then replays the tail. A corrupt analytics snapshot can be discarded only if a complete event range or a known-consistent older snapshot exists. Missing/corrupt control is fatal. A crash during backup leaves the prior archive authoritative; two SQLite files are not atomically committed together. Backup v1 remains the only production format in M010.

Current dashboard/statistics/traces read one database snapshot through `DashboardRepository::{timeseries_json,grouped_timeseries_json,observability_stats,load}`, `UsageRollupRepository::{get_all_usage_windows,summary,dashboard_summary,dashboard_summary_basic}`, `RequestRepository::{get_by_id,list_recent}`, and provider-ping recent/latest reads. `load` composes account/model/catalog facts, active reservations, account/operational events, recent requests/attempts and usage data. Active reservations, pending requests, current accounts/models/providers, catalog/quarantine/backoff/pricing and compression policy state require strong/live control reads. Historical request lists/details, attempts, routing traces, usage/time series, pings, account/operational events and rollups could be eventually consistent in Option C, but all currently expose synchronous post-finalization visibility. An attempt/trace must be withheld until its corresponding projection event is applied; otherwise the attempt/decision pair breaks the existing invariant. No maximum acceptable lag or degraded response behavior exists today, so an eventual-read split changes observable semantics. The investigation recommends that any future redesign preserve response schemas but explicitly version a freshness contract and measure it; it cannot assume current compatibility automatically.

## 7. Migration and rollback outline

No migration is authorized. A safe future sequence would: (1) create control/outbox and analytics schemas additively while schema 54 remains authoritative; (2) take a consistent schema-54 snapshot and seed analytics with a deterministic high-water cursor; (3) dual-record new projection events transactionally in the old database only, then shadow-apply to the new analytics file; (4) compare exact normalized dashboard/trace results and retention over representative fixtures; (5) create a coordinated backup containing both snapshots and tail; (6) switch read paths behind a versioned release boundary while retaining old tables/writes; (7) only after sustained parity, stop old routing/history writes; (8) retain a rollback window where the monolithic source remains complete; (9) later compact/retire old data in a separately backed-up migration. Downgrade before step 7 reads monolith; after step 7 requires replaying the durable event stream back into monolith or restoring a pre-cutover backup and losing post-backup history. Destructive flag-day conversion is rejected. Retention must be shadow-compared and cursor-aware before cutover.

## 8. Deterministic failure-state matrix

| Failure point | Authority / data loss | Retry or recovery | May inference continue? |
|---|---|---|---|
| Before control commit | Old control state; no event exists | SQLite rollback; retry publication/finalization under existing idempotency rules | Yes, subject to current control DB health |
| After control commit, before projector wake | Control + outbox durable; analytics lags | Startup scan resumes ordered outbox; wake is only an optimization | Yes until the finite backlog policy is reached |
| While reading outbox | Control + outbox unchanged | Read transaction/batch retries | Yes, subject to cap |
| Analytics apply before commit | Analytics transaction rolls back, cursor unchanged | Replay same event | Yes, subject to cap |
| After analytics commit before next loop/ack | Projection and cursor both durable | Replay sees event id <= cursor / unique event id and has no second effect | Yes |
| Duplicate delivery | Control unchanged; event is stable | Unique event id and cursor make effect idempotent | Yes |
| Shutdown cancellation | Outbox is source; uncommitted analytics batch rolls back | Process supervisor cancels and startup scanner resumes | Yes until cap |
| Analytics busy/locked | No cursor advance | Bounded retry/backoff; no spin | Yes until cap |
| Analytics corrupt/unopenable | Control authoritative; analytics untrusted | Restore coordinated archive or rebuild from retained event range | Yes only until cap; no silent bypass |
| Control recovery while analytics lags | Control recovery rows decide pending request/attempt/reservation outcome | Recovery terminal event appended in same repair transaction | Yes until cap |
| Backup while lagged | Control plus unapplied tail is authority | Capture cursor/high-water/tail under pause or quiescence contract | Existing runtime continues under current monolith; split behavior unresolved |
| Restore cursor behind control | Control/outbox is authoritative | Replay after cursor | Yes after validated restore |
| Cursor ahead/invalid | No trustworthy projection order | Fail restore closed; choose prior verified backup or rebuild | No split startup until repaired |
| Old event schema pending during upgrade/downgrade | Control event bytes are durable authority | Reader supports old versions for full retention window; unsupported events halt at cursor | Yes only until cap; compatibility window has not been costed |

## 9. Prototype and evidence interpretation

`rust/tests/persistence_projection_architecture.rs` uses temporary file-backed SQLite files and a deterministic 100-request model. It counts modeled indexes and rows, reports database and WAL file deltas, keeps analytics apply separate, and tests: interruption before projection commit; process restart; duplicate replay; cursor persistence; reclaim only through durable cursor; control state surviving without analytics; and poison-event cursor non-advance. It introduces no production migration, configuration, dependency, task, API, or reusable runtime type.

The prototype's 12-vs-2 explicit secondary-index count is a structural comparison of the declared measured subset. Its local run recorded 77,824 bytes of baseline main-file growth and 4,161,232 WAL bytes, versus 49,152 and 4,140,632 bytes for candidate control storage; the candidate also wrote 200 outbox rows to a separate file, whose analytics bytes are intentionally not counted as foreground. There are 700 baseline versus 800 candidate modeled foreground SQL statements and row mutations in 100 request lifecycles. Baseline inserts/updates indexed request, reservation, attempt, and routing families; candidate synchronously mutates minimal request/reservation/attempt control state and inserts one outbox event for each publication/finalization. It does not include catalog/config tables or all production request columns/indexes and therefore cannot produce an absolute production write reduction. The comparison supports a future narrowly scoped index/field write attribution study, not Pi performance acceptance. Per-thread worker `write_bytes` was not measured: the synchronous isolated prototype has no tokio-rusqlite worker, and M008's separate proxy cohort is not reusable without changing what is measured. M007/M008 physical evidence remains directional at journal/worker level only.

## 10. Decision and follow-up

| Dimension | A: monolith | B: same DB logical split | C: control/outbox + analytics |
|---|---|---|---|
| Correctness/recovery authority | Existing, demonstrated | Preservable with careful field split | Preservable only if control and event append stay atomic |
| Request-path secondary indexes | Current full set | Could be reduced if history indexes move, but same-file writes retain checkpoint coupling | Lower modeled index fanout; outbox adds payload bytes/PK write |
| Bounded resources | Existing retention and one DB | One file; straightforward | No bounded lossless outage policy under unchanged history contract |
| Backup/restore | Existing v1 | Existing one-file | Two snapshots plus retained tail/cursor contract, no cross-file atomic commit |
| Read consistency/API | Current immediate visibility | Immediate in same file | Current history views become eventual unless requests block |
| Decision | retain | possible future index-only study | reject for current contract |

No ADR-0002 is proposed because the candidate has an unresolved hard resource/compatibility contradiction. Any successor should first define the product's finite retention and analytics-outage contract, including which existing histories may be unavailable or expired and whether admission may be refused at a declared backlog ceiling. Without that explicit contract, physical target qualification or production split implementation is premature. This is a finding, not an unblocked implementation plan.
