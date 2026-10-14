PRAGMA foreign_keys=OFF;
BEGIN TRANSACTION;
CREATE TABLE _migrations (
version INTEGER PRIMARY KEY,
name TEXT NOT NULL,
applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO _migrations VALUES(1,'0001_initial.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(2,'0002_indexes.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(3,'0003_request_attempts.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(4,'0004_integration_hardening.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(5,'0005_price_microdollars.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(6,'0006_correct_price_microdollars.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(7,'0007_price_cache_rates.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(8,'0008_proxy_request_identity.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(9,'0009_model_protocol_source.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(10,'0010_health_probe.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(11,'0011_model_resolution_status.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(12,'0012_drop_reservations_estimated_microdollars.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(13,'0013_request_attempts_account_id_index.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(14,'0014_bandwidth_tracking.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(15,'0015_multi_provider.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(16,'0016_requests_provider_id.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(17,'0017_price_snapshots_provider_id.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(18,'0018_provider_pings.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(19,'0019_client_ip.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(20,'0020_performance_indexes.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(21,'0021_provider_model_metadata.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(22,'0022_dashboard_indexes.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(23,'0023_deprecated_model_placeholder.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(24,'0024_account_backoffs.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(25,'0025_stale_request_index.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(26,'0026_attempt_observability.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(27,'0027_routing_decisions.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(28,'0028_operational_events.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(29,'0029_latency_phases.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(30,'0030_model_pricing_aliases.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(31,'0031_price_snapshot_provenance.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(32,'0032_usage_rollups.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(33,'0033_request_provider_local_cost.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(34,'0034_transcoding_daily.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(35,'0035_routing_decision_score_components.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(36,'0036_model_info.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(37,'0037_model_info_failure_count.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(38,'0038_model_info_phase5.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(39,'0039_thinking_observability.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(40,'0040_cache_token_observability.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(41,'0041_canonical_request_segmentation.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(42,'0042_observe_mode_compression_accounting.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(43,'0043_safe_suffix_compression.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(44,'0044_compression_policy_resolver.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(45,'0045_synthetic_cache_controls.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(46,'0046_closed_loop_threshold_tuning.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(47,'0047_normalize_rollup_bucket_start.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(48,'0048_request_cost_repairs.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(49,'0049_model_info_match_evidence.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(50,'0050_routing_decisions_retention_index.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(51,'0051_model_quarantine.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(52,'0052_catalog_refresh_state.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(53,'0053_remove_attempt_status_analytics_index.sql','2026-10-14 16:14:21');
INSERT INTO _migrations VALUES(54,'0054_model_quarantine_null_identity.sql','2026-10-14 16:14:21');
CREATE TABLE accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    api_key_env TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    weight REAL NOT NULL DEFAULT 1.0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
, provider_id TEXT NOT NULL DEFAULT 'opencode-go');
INSERT INTO accounts VALUES(1,'account-a','D001_ACCOUNT_A_KEY',1,2.0,'2026-10-14 16:14:21','provider-a');
INSERT INTO accounts VALUES(2,'account-b','D001_ACCOUNT_B_KEY',1,1.0,'2026-10-14 16:14:21','provider-b');
INSERT INTO accounts VALUES(3,'disabled-a','D001_DISABLED_KEY',0,1.0,'2026-10-14 16:14:21','provider-a');
CREATE TABLE models (
    model_id TEXT PRIMARY KEY,
    display_name TEXT,
    protocol TEXT NOT NULL DEFAULT 'openai',
    capabilities TEXT NOT NULL DEFAULT '{}',
    source_metadata TEXT NOT NULL DEFAULT '{}',
    first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
, protocol_source TEXT, endpoint_path TEXT, resolution_status TEXT NOT NULL DEFAULT 'resolved', provider_id TEXT NOT NULL DEFAULT 'opencode-go');
INSERT INTO models VALUES('__deprecated__','Deprecated models','openai','{}','{}','2026-10-14 16:14:21','2026-10-14 16:14:21',NULL,NULL,'resolved','opencode-go');
INSERT INTO models VALUES('shared-model','Shared Model','openai','{"supports_tools":true}','{"fixture":"d001"}','2026-10-14 16:14:21','2026-10-14 16:14:21','config',NULL,'resolved','provider-a');
INSERT INTO models VALUES('withdrawn-model','Withdrawn Model','openai','{}','{"fixture":"d001"}','2026-10-14 16:14:21','2026-10-14 16:14:21','config',NULL,'resolved','provider-a');
CREATE TABLE account_models (
    account_id INTEGER NOT NULL,
    model_id TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (account_id, model_id),
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE,
    FOREIGN KEY (model_id) REFERENCES models(model_id) ON DELETE CASCADE
);
INSERT INTO account_models VALUES(1,'shared-model',1,'2026-10-14 16:14:21');
INSERT INTO account_models VALUES(2,'shared-model',1,'2026-10-14 16:14:21');
INSERT INTO account_models VALUES(1,'withdrawn-model',1,'2026-10-14 16:14:21');
CREATE TABLE requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    model_id TEXT NOT NULL,
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    status TEXT NOT NULL DEFAULT 'pending',
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cost_microdollars INTEGER DEFAULT 0,
    upstream_latency_ms REAL DEFAULT 0,
    error_message TEXT, protocol TEXT NOT NULL DEFAULT 'openai', streamed INTEGER NOT NULL DEFAULT 0, exactness TEXT NOT NULL DEFAULT 'unknown', cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER, thinking_characters INTEGER, reserved_microdollars INTEGER NOT NULL DEFAULT 0, first_byte_ms INTEGER, retry_count INTEGER NOT NULL DEFAULT 0, upstream_request_id TEXT, error_class TEXT, error_detail TEXT, status_code INTEGER, proxy_request_id TEXT, bytes_received INTEGER DEFAULT 0, bytes_emitted INTEGER DEFAULT 0, provider_id TEXT NOT NULL DEFAULT 'opencode-go', client_ip TEXT, original_model_id TEXT, first_attempt_at TIMESTAMP, last_attempt_id INTEGER, upstream_connect_ms INTEGER, upstream_read_ms INTEGER, coordinator_overhead_ms INTEGER, provider_cost_microdollars INTEGER, provider_cost_source TEXT, local_cost_microdollars INTEGER, local_cost_exactness TEXT, upstream_protocol TEXT, thinking_trace_json TEXT, cache_counter_status TEXT NOT NULL DEFAULT 'not_reported', cached_input_tokens INTEGER, cache_read_input_tokens INTEGER, cache_creation_input_tokens INTEGER, cache_write_input_tokens INTEGER, cache_write_input_reported INTEGER, input_tokens_reported INTEGER, output_tokens_reported INTEGER, total_tokens_reported INTEGER, request_shape_hash TEXT, stable_prefix_hash TEXT, transcoded INTEGER NOT NULL DEFAULT 0, raw_usage_json TEXT, segmentation_status TEXT NOT NULL DEFAULT 'empty_request', stable_prefix_estimated_tokens INTEGER, semi_stable_estimated_tokens INTEGER, volatile_estimated_tokens INTEGER, stable_prefix_bytes INTEGER, semi_stable_bytes INTEGER, volatile_bytes INTEGER, segmentation_summary_json TEXT, compression_status TEXT NOT NULL DEFAULT 'disabled', compression_mode TEXT, compression_candidate_count INTEGER NOT NULL DEFAULT 0, compression_eligible_candidate_count INTEGER NOT NULL DEFAULT 0, compression_suppressed_candidate_count INTEGER NOT NULL DEFAULT 0, compression_estimated_original_tokens INTEGER, compression_estimated_compressed_tokens INTEGER, compression_estimated_savings_tokens INTEGER, compression_analyzer_latency_ms REAL, compression_warning_count INTEGER NOT NULL DEFAULT 0, compression_reason_code_counts_json TEXT, compression_summary_json TEXT, compression_applied INTEGER NOT NULL DEFAULT 0, compression_transform_count INTEGER NOT NULL DEFAULT 0, compression_transforms_by_reason_json TEXT, compression_original_tokens INTEGER, compression_compressed_tokens INTEGER, compression_savings_tokens INTEGER, compression_pre_stable_prefix_hash TEXT, compression_post_stable_prefix_hash TEXT, compression_stable_prefix_preserved INTEGER NOT NULL DEFAULT 1, compression_warnings_json TEXT, compression_latency_ms REAL NOT NULL DEFAULT 0, compression_failed_fallback INTEGER NOT NULL DEFAULT 0, compression_applied_summary_json TEXT, compression_policy_name TEXT, compression_policy_source TEXT, compression_policy_warnings_json TEXT, synthetic_cache_status TEXT, synthetic_cache_dry_run INTEGER NOT NULL DEFAULT 1, synthetic_cache_candidate_count INTEGER NOT NULL DEFAULT 0, synthetic_cache_applied_count INTEGER NOT NULL DEFAULT 0, synthetic_cache_warning_count INTEGER NOT NULL DEFAULT 0, synthetic_cache_warnings_json TEXT, synthetic_cache_policy_name TEXT, synthetic_cache_policy_source TEXT, synthetic_cache_summary_json TEXT,
    FOREIGN KEY (account_id) REFERENCES accounts(id),
    FOREIGN KEY (model_id) REFERENCES models(model_id)
);
INSERT INTO requests VALUES(1,1,'shared-model','2026-10-14 16:14:21',NULL,'success',100,50,700,0.0,NULL,'openai',0,'unknown',NULL,NULL,NULL,NULL,0,NULL,0,NULL,NULL,NULL,NULL,NULL,0,0,'provider-a',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'not_reported',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,0,NULL,'empty_request',NULL,NULL,NULL,NULL,NULL,NULL,NULL,'disabled',NULL,0,0,0,NULL,NULL,NULL,NULL,0,NULL,NULL,0,0,NULL,NULL,NULL,NULL,NULL,NULL,1,NULL,0.0,0,NULL,NULL,NULL,NULL,NULL,1,0,0,0,NULL,NULL,NULL,NULL);
INSERT INTO requests VALUES(2,2,'shared-model','2026-10-14 16:14:21',NULL,'pending',20,10,100,0.0,NULL,'anthropic',1,'unknown',NULL,NULL,NULL,NULL,0,NULL,0,NULL,NULL,NULL,NULL,NULL,0,0,'provider-b',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,'not_reported',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,0,NULL,'empty_request',NULL,NULL,NULL,NULL,NULL,NULL,NULL,'disabled',NULL,0,0,0,NULL,NULL,NULL,NULL,0,NULL,NULL,0,0,NULL,NULL,NULL,NULL,NULL,NULL,1,NULL,0.0,0,NULL,NULL,NULL,NULL,NULL,1,0,0,0,NULL,NULL,NULL,NULL);
CREATE TABLE reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    model_id TEXT NOT NULL,
    reserved_microdollars INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    released_at TIMESTAMP,
    status TEXT NOT NULL DEFAULT 'active', estimated_tokens INTEGER NOT NULL DEFAULT 0, expires_at TIMESTAMP, release_reason TEXT, original_model_id TEXT,
    FOREIGN KEY (request_id) REFERENCES requests(id),
    FOREIGN KEY (account_id) REFERENCES accounts(id),
    FOREIGN KEY (model_id) REFERENCES models(model_id)
);
INSERT INTO reservations VALUES(1,1,1,'shared-model',700,'2026-10-14 16:14:21',NULL,'released',0,NULL,NULL,NULL);
INSERT INTO reservations VALUES(2,2,2,'shared-model',500,'2026-10-14 16:14:21',NULL,'active',0,NULL,NULL,NULL);
CREATE TABLE model_price_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id TEXT NOT NULL,
    input_price_per_1k REAL,
    output_price_per_1k REAL,
    captured_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, input_per_million_microdollars INTEGER, output_per_million_microdollars INTEGER, source TEXT NOT NULL DEFAULT 'config', metadata_json TEXT, cache_read_per_million_microdollars INTEGER, cache_write_per_million_microdollars INTEGER, provider_id TEXT NOT NULL DEFAULT 'opencode-go', source_detail TEXT, source_confidence TEXT, catalog_source TEXT,
    FOREIGN KEY (model_id) REFERENCES models(model_id)
);
CREATE TABLE account_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    details TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
CREATE TABLE request_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL,
    attempt_number INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    status_code INTEGER,
    error_class TEXT,
    upstream_request_id TEXT, bytes_emitted INTEGER NOT NULL DEFAULT 0, error_detail TEXT, provider_id TEXT, model_id TEXT, protocol TEXT, retry_category TEXT, release_reason TEXT, bytes_received INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL DEFAULT 0, streamed INTEGER NOT NULL DEFAULT 0, is_retry_outcome INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (request_id) REFERENCES requests(id) ON DELETE CASCADE,
    FOREIGN KEY (account_id) REFERENCES accounts(id)
);
CREATE TABLE health_probe (
    id INTEGER PRIMARY KEY,
    probe_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE providers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL UNIQUE,
    base_url TEXT NOT NULL,
    protocols TEXT NOT NULL DEFAULT '["openai"]',
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT INTO providers VALUES(1,'opencode-go','https://opencode.ai/zen/go/v1','["openai", "anthropic"]',1,'2026-10-14 16:14:21');
INSERT INTO providers VALUES(10,'provider-a','https://provider-a.invalid/v1','["openai","anthropic"]',1,'2026-10-14 16:14:21');
INSERT INTO providers VALUES(11,'provider-b','https://provider-b.invalid/v1','["openai"]',1,'2026-10-14 16:14:21');
CREATE TABLE provider_pings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL,
    account_name TEXT NOT NULL,
    probed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    latency_ms INTEGER,
    status_code INTEGER,
    error TEXT,
    model_count INTEGER DEFAULT 0
);
CREATE TABLE provider_model_metadata (
    model_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    display_name TEXT,
    protocol TEXT,
    capabilities TEXT NOT NULL DEFAULT '{}',
    source_metadata TEXT NOT NULL DEFAULT '{}',
    protocol_source TEXT,
    first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolution_status TEXT NOT NULL DEFAULT 'resolved',
    PRIMARY KEY (model_id, provider_id),
    FOREIGN KEY (model_id) REFERENCES models(model_id) ON DELETE CASCADE
);
INSERT INTO provider_model_metadata VALUES('shared-model','provider-a','Shared Model','openai','{"supports_tools":true}','{"fixture":"d001"}','config','2026-10-14 16:14:21','2026-10-14 16:14:21','resolved');
INSERT INTO provider_model_metadata VALUES('shared-model','provider-b','Shared Model','anthropic','{"supports_tools":true}','{"fixture":"d001"}','config','2026-10-14 16:14:21','2026-10-14 16:14:21','resolved');
INSERT INTO provider_model_metadata VALUES('withdrawn-model','provider-a','Withdrawn Model','openai','{}','{"fixture":"d001"}','config','2026-10-14 16:14:21','2026-10-14 16:14:21','resolved');
CREATE TABLE account_backoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    model_id TEXT,
    reason TEXT NOT NULL,
    status_code INTEGER,
    error_class TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 1,
    backoff_until TEXT,
    last_failure_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(account_id, model_id, reason)
);
INSERT INTO account_backoffs VALUES(1,1,NULL,'rate_limited',429,'rate_limited',2,'2026-09-04 00:10:00','2026-09-04 00:00:00','2026-09-04 00:00:00');
INSERT INTO account_backoffs VALUES(2,1,'withdrawn-model','model_unavailable',404,'model_not_found',1,'2026-09-04 00:05:00','2026-09-04 00:00:00','2026-09-04 00:00:00');
CREATE TABLE routing_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL,
    attempt_number INTEGER NOT NULL,
    model_id TEXT NOT NULL,
    provider_id TEXT,
    protocol TEXT,
    selected_account_id INTEGER,
    selected_account_name TEXT,
    selected_tier INTEGER,
    selected_score REAL,
    eligible_count INTEGER NOT NULL DEFAULT 0,
    scored_count INTEGER NOT NULL DEFAULT 0,
    attempted_excluded_count INTEGER NOT NULL DEFAULT 0,
    top_score REAL,
    top_score_account_name TEXT,
    exclude_reasons_json TEXT NOT NULL DEFAULT '[]',
    decision_made_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, score_components_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (request_id) REFERENCES requests(id) ON DELETE CASCADE,
    FOREIGN KEY (selected_account_id) REFERENCES accounts(id)
);
CREATE TABLE operational_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    occurred_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE model_pricing_aliases (
    provider_id TEXT NOT NULL,
    upstream_model_id TEXT NOT NULL,
    catalog_source TEXT NOT NULL,
    catalog_model_id TEXT NOT NULL,
    confidence TEXT NOT NULL,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (provider_id, upstream_model_id, catalog_source)
);
CREATE TABLE usage_rollups (
    bucket_start TEXT NOT NULL,
    bucket_size_s INTEGER NOT NULL,
    provider_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    account_id INTEGER NOT NULL DEFAULT 0,
    protocol TEXT NOT NULL,
    streamed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    thinking_characters INTEGER NOT NULL DEFAULT 0,
    cost_microdollars INTEGER NOT NULL DEFAULT 0,
    bytes_received INTEGER NOT NULL DEFAULT 0,
    bytes_emitted INTEGER NOT NULL DEFAULT 0,
    latency_ms_sum INTEGER NOT NULL DEFAULT 0,
    latency_ms_min INTEGER,
    latency_ms_max INTEGER,
    first_byte_ms_sum INTEGER NOT NULL DEFAULT 0,
    first_byte_ms_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (
        bucket_start,
        bucket_size_s,
        provider_id,
        model_id,
        account_id,
        protocol,
        streamed,
        status
    )
);
CREATE TABLE transcoding_daily (
    day TEXT NOT NULL,                          -- YYYY-MM-DD UTC
    client_protocol TEXT NOT NULL,
    upstream_protocol TEXT NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0,
    loss_warning_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, client_protocol, upstream_protocol)
) WITHOUT ROWID;
CREATE TABLE model_info_canonical (
    model_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    summary TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    provenance_json TEXT NOT NULL DEFAULT '{}',
    conflicts_json TEXT NOT NULL DEFAULT '{}',
    sparse INTEGER NOT NULL DEFAULT 0,
    first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_refreshed_at TIMESTAMP,
    next_refresh_at TIMESTAMP,
    FOREIGN KEY (model_id) REFERENCES models(model_id) ON DELETE CASCADE
);
CREATE TABLE model_info_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id TEXT,
    provider_id TEXT,
    source TEXT NOT NULL,
    source_model_id TEXT NOT NULL,
    observed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP,
    confidence REAL NOT NULL DEFAULT 0.5,
    raw_hash TEXT NOT NULL,
    normalized_json TEXT NOT NULL DEFAULT '{}',
    raw_json TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (model_id) REFERENCES models(model_id) ON DELETE CASCADE,
    UNIQUE(source, source_model_id, raw_hash)
);
CREATE TABLE model_info_aliases (
    model_id TEXT NOT NULL,
    provider_id TEXT,
    alias TEXT NOT NULL,
    source TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 0.5,
    active INTEGER NOT NULL DEFAULT 1,
    first_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, notes TEXT, match_method TEXT, discovered_by TEXT, diagnostics_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (provider_id, alias, source),
    FOREIGN KEY (model_id) REFERENCES models(model_id) ON DELETE CASCADE
);
CREATE TABLE model_info_source_health (
    source TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_success_at TIMESTAMP,
    last_error_at TIMESTAMP,
    last_error_class TEXT,
    last_error_message TEXT,
    cooldown_until TIMESTAMP
, failure_count INTEGER NOT NULL DEFAULT 0, last_status_code INTEGER, rate_limited_until TIMESTAMP, last_success_duration_ms INTEGER, last_payload_count INTEGER);
CREATE TABLE model_info_overrides (
    model_id TEXT PRIMARY KEY,
    summary TEXT,
    family TEXT,
    display_name TEXT,
    notes TEXT,
    hide_benchmark_sources INTEGER NOT NULL DEFAULT 0,
    status_override TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (model_id) REFERENCES models(model_id) ON DELETE CASCADE
);
CREATE TABLE compression_tuning_recommendations (
    policy_name TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    recommendation_json TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE compression_tuning_overrides (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_name TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    reason_codes_json TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    expires_at TEXT
);
CREATE TABLE request_cost_repairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL
        REFERENCES requests(id) ON DELETE CASCADE,
    old_cost_microdollars INTEGER NOT NULL,
    new_cost_microdollars INTEGER NOT NULL,
    old_exactness TEXT,
    new_exactness TEXT NOT NULL,
    reason TEXT NOT NULL,
    provider_filter TEXT,
    since_date TEXT,
    repaired_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE model_info_match_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id TEXT NOT NULL,
    provider_id TEXT,
    source TEXT NOT NULL,
    alias TEXT NOT NULL,
    match_method TEXT NOT NULL,
    confidence REAL NOT NULL,
    diagnostics_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (model_id) REFERENCES models(model_id) ON DELETE CASCADE
);
CREATE TABLE model_quarantine (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    canonical_model_id TEXT NOT NULL,
    upstream_model_id TEXT,
    upstream_protocol TEXT NOT NULL DEFAULT 'openai',
    state TEXT NOT NULL DEFAULT 'suspected',
    evidence_provenance TEXT NOT NULL DEFAULT 'runtime_http',
    reason TEXT NOT NULL DEFAULT '',
    first_observed TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_observed TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    observation_count INTEGER NOT NULL DEFAULT 1,
    expiry TEXT,
    cleared_at TEXT,
    clear_reason TEXT,
    last_status_code INTEGER,
    last_error_class TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(provider_id, account_id, canonical_model_id, upstream_model_id, upstream_protocol)
);
INSERT INTO model_quarantine VALUES(1,'provider-a','account-a','withdrawn-model','withdrawn-model','openai','quarantined','runtime_http','model_unavailable','2026-09-04 00:00:00','2026-09-04 00:01:00',2,'2026-09-04 00:06:00',NULL,NULL,404,'model_not_found','2026-10-14 16:14:21');
INSERT INTO model_quarantine VALUES(2,'provider-b','account-b','shared-model',NULL,'anthropic','suspected','provider_catalog','partial_refresh','2026-09-04 00:00:00','2026-09-04 00:00:00',1,'2026-09-04 00:02:00',NULL,NULL,NULL,NULL,'2026-10-14 16:14:21');
CREATE TABLE catalog_refresh_state (
    account_id INTEGER PRIMARY KEY,
    provider_id TEXT NOT NULL,
    last_successful_refresh_at TIMESTAMP NOT NULL,
    last_outcome TEXT NOT NULL DEFAULT 'legacy_hydrated',
    model_count INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
);
INSERT INTO catalog_refresh_state VALUES(1,'provider-a','2026-09-04 00:00:00','success_authoritative',2);
INSERT INTO catalog_refresh_state VALUES(2,'provider-b','2026-09-04 00:00:00','success_authoritative',1);
PRAGMA writable_schema=ON;
CREATE TABLE IF NOT EXISTS sqlite_sequence(name,seq);
DELETE FROM sqlite_sequence;
INSERT INTO sqlite_sequence VALUES('providers',11);
INSERT INTO sqlite_sequence VALUES('accounts',3);
INSERT INTO sqlite_sequence VALUES('requests',2);
INSERT INTO sqlite_sequence VALUES('reservations',2);
INSERT INTO sqlite_sequence VALUES('account_backoffs',2);
INSERT INTO sqlite_sequence VALUES('model_quarantine',2);
CREATE INDEX idx_accounts_enabled ON accounts(enabled);
CREATE INDEX idx_account_models_account ON account_models(account_id);
CREATE INDEX idx_account_models_model ON account_models(model_id);
CREATE INDEX idx_requests_account ON requests(account_id);
CREATE INDEX idx_requests_model ON requests(model_id);
CREATE INDEX idx_requests_started ON requests(started_at);
CREATE INDEX idx_reservations_account ON reservations(account_id);
CREATE INDEX idx_reservations_model ON reservations(model_id);
CREATE INDEX idx_reservations_status ON reservations(status);
CREATE INDEX idx_reservations_request ON reservations(request_id);
CREATE INDEX idx_model_price_snapshots_model ON model_price_snapshots(model_id);
CREATE INDEX idx_model_price_snapshots_captured ON model_price_snapshots(captured_at);
CREATE INDEX idx_account_events_account ON account_events(account_id);
CREATE INDEX idx_account_events_type ON account_events(event_type);
CREATE INDEX idx_account_events_created ON account_events(created_at);
CREATE INDEX idx_request_attempts_request
    ON request_attempts(request_id);
CREATE INDEX idx_request_attempts_account
    ON request_attempts(account_id, started_at);
CREATE INDEX idx_requests_exactness ON requests(exactness);
CREATE INDEX idx_requests_completed ON requests(completed_at);
CREATE INDEX idx_requests_protocol ON requests(protocol);
CREATE INDEX idx_reservations_expires ON reservations(expires_at);
CREATE INDEX idx_requests_status_started ON requests(status, started_at);
CREATE INDEX idx_price_snapshots_model_source
    ON model_price_snapshots(model_id, source);
CREATE UNIQUE INDEX idx_requests_proxy_request_id
    ON requests(proxy_request_id);
CREATE INDEX idx_provider_pings_provider ON provider_pings(provider_id);
CREATE INDEX idx_provider_pings_probed ON provider_pings(probed_at);
CREATE INDEX idx_provider_pings_provider_probed
    ON provider_pings(provider_id, probed_at);
CREATE INDEX idx_requests_account_started
    ON requests(account_id, started_at);
CREATE INDEX idx_price_snapshots_model_provider_captured
    ON model_price_snapshots(model_id, provider_id, captured_at DESC, id DESC);
CREATE INDEX idx_requests_streamed_provider_model_started_ttft
    ON requests(provider_id, model_id, started_at, first_byte_ms)
    WHERE streamed = 1 AND first_byte_ms IS NOT NULL;
CREATE INDEX idx_reservations_status_expires
    ON reservations(status, expires_at);
CREATE INDEX idx_provider_model_metadata_provider
    ON provider_model_metadata(provider_id, model_id);
CREATE INDEX idx_requests_client_ip_started
    ON requests(client_ip, started_at);
CREATE INDEX idx_requests_streamed_started_ttft
    ON requests(started_at, first_byte_ms)
    WHERE streamed = 1 AND first_byte_ms IS NOT NULL;
CREATE INDEX idx_requests_original_model_id
    ON requests(original_model_id) WHERE original_model_id IS NOT NULL;
CREATE INDEX idx_reservations_original_model_id
    ON reservations(original_model_id) WHERE original_model_id IS NOT NULL;
CREATE INDEX idx_account_backoffs_active
    ON account_backoffs(backoff_until);
CREATE INDEX idx_account_backoffs_account_model
    ON account_backoffs(account_id, model_id);
CREATE INDEX idx_request_attempts_provider_started
    ON request_attempts(provider_id, started_at);
CREATE INDEX idx_request_attempts_model_started
    ON request_attempts(model_id, started_at);
CREATE INDEX idx_request_attempts_retry_category_started
    ON request_attempts(retry_category, started_at);
CREATE INDEX idx_routing_decisions_request
    ON routing_decisions(request_id);
CREATE INDEX idx_routing_decisions_model_started
    ON routing_decisions(model_id, decision_made_at);
CREATE INDEX idx_routing_decisions_provider_started
    ON routing_decisions(provider_id, decision_made_at);
CREATE INDEX idx_routing_decisions_selected_account
    ON routing_decisions(selected_account_name, decision_made_at);
CREATE INDEX idx_operational_events_type_occurred
    ON operational_events(event_type, occurred_at);
CREATE INDEX idx_operational_events_occurred
    ON operational_events(occurred_at);
CREATE INDEX idx_requests_ttfb_ms
    ON requests(first_byte_ms)
    WHERE first_byte_ms IS NOT NULL;
CREATE INDEX idx_pricing_aliases_catalog
    ON model_pricing_aliases(catalog_source, catalog_model_id);
CREATE INDEX idx_pricing_aliases_provider
    ON model_pricing_aliases(provider_id, upstream_model_id);
CREATE INDEX idx_usage_rollups_bucket ON usage_rollups(bucket_start, bucket_size_s);
CREATE INDEX idx_usage_rollups_provider_model ON usage_rollups(provider_id, model_id, bucket_start);
CREATE INDEX idx_usage_rollups_account ON usage_rollups(account_id, bucket_start);
CREATE INDEX idx_model_info_canonical_status
    ON model_info_canonical(status);
CREATE INDEX idx_model_info_canonical_next_refresh
    ON model_info_canonical(next_refresh_at);
CREATE INDEX idx_model_info_observations_model_source
    ON model_info_observations(model_id, source);
CREATE INDEX idx_model_info_observations_source_model
    ON model_info_observations(source, source_model_id);
CREATE INDEX idx_model_info_aliases_alias
    ON model_info_aliases(alias);
CREATE INDEX idx_requests_cache_counter_status
    ON requests(cache_counter_status, started_at);
CREATE INDEX idx_requests_transcoded
    ON requests(transcoded, started_at);
CREATE INDEX idx_requests_segmentation_status
    ON requests(segmentation_status, started_at);
CREATE INDEX idx_requests_compression_status
    ON requests(compression_status, started_at);
CREATE INDEX idx_requests_compression_mode
    ON requests(compression_mode, started_at);
CREATE INDEX idx_requests_compression_applied
    ON requests(compression_applied, started_at);
CREATE INDEX idx_requests_compression_policy_name
    ON requests(compression_policy_name, started_at);
CREATE INDEX idx_requests_synthetic_cache_status
    ON requests(synthetic_cache_status, started_at);
CREATE INDEX idx_requests_synthetic_cache_policy_name
    ON requests(synthetic_cache_policy_name, started_at);
CREATE INDEX idx_compression_tuning_recommendations_status
    ON compression_tuning_recommendations(status, generated_at);
CREATE INDEX idx_compression_tuning_overrides_policy
    ON compression_tuning_overrides(policy_name, generated_at);
CREATE INDEX idx_request_cost_repairs_request
    ON request_cost_repairs(request_id, repaired_at DESC);
CREATE INDEX idx_request_cost_repairs_repaired_at
    ON request_cost_repairs(repaired_at DESC);
CREATE INDEX idx_model_info_match_evidence_model
    ON model_info_match_evidence(model_id, source);
CREATE INDEX idx_model_info_match_evidence_source
    ON model_info_match_evidence(source, last_seen_at DESC);
CREATE INDEX idx_routing_decisions_retention
    ON routing_decisions(decision_made_at, id);
CREATE INDEX idx_model_quarantine_state
    ON model_quarantine(state);
CREATE INDEX idx_model_quarantine_expiry
    ON model_quarantine(expiry);
CREATE INDEX idx_model_quarantine_model
    ON model_quarantine(canonical_model_id);
CREATE INDEX idx_catalog_refresh_state_provider
    ON catalog_refresh_state(provider_id);
CREATE UNIQUE INDEX uq_model_quarantine_null_upstream
    ON model_quarantine(
        provider_id,
        account_id,
        canonical_model_id,
        upstream_protocol
    )
    WHERE upstream_model_id IS NULL;
PRAGMA writable_schema=OFF;
COMMIT;
