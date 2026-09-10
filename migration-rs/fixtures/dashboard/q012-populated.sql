-- Q012 dashboard fixture corpus, version 1.
--
-- This is intentionally a data-only, secret-free fixture. The qualification
-- runner applies the canonical schema first, records every migration in the
-- normal ledger, and then executes this file once before copying the exact
-- SQLite bytes to both implementations.

DELETE FROM providers WHERE provider_id = 'opencode-go';

INSERT INTO providers (id, provider_id, base_url, protocols, enabled)
VALUES
  (2, 'q012-alpha', 'https://fixture.invalid/alpha', '["openai","anthropic"]', 1),
  (3, 'q012-beta', 'https://fixture.invalid/beta', '["openai"]', 1);

INSERT INTO accounts (id, name, api_key_env, enabled, weight, provider_id)
VALUES
  (1, 'alpha-main', 'Q012_ALPHA_MAIN_KEY', 1, 1.0, 'q012-alpha'),
  (2, 'alpha-éclair', 'Q012_ALPHA_ECLAIR_KEY', 1, 0.75, 'q012-alpha'),
  (3, 'beta-long-account-✨', 'Q012_BETA_LONG_KEY', 1, 1.25, 'q012-beta');

INSERT INTO models
  (model_id, display_name, protocol, capabilities, source_metadata,
   protocol_source, resolution_status, provider_id)
VALUES
  ('q012-chat-model', 'Q012 Chat Model', 'openai', '{"streaming":true}', '{"source":"fixture"}', 'fixture', 'available', 'q012-alpha'),
  ('q012-escape-模型<&"', 'Q012 Escape <模型> & "quoted"', 'anthropic', '{"thinking":true}', '{"source":"fixture"}', 'fixture', 'available', 'q012-alpha'),
  ('q012-error-model', 'Q012 Error Model', 'openai', '{}', '{"source":"fixture"}', 'fixture', 'configured', 'q012-beta');

INSERT INTO account_models (account_id, model_id, enabled)
VALUES
  (1, 'q012-chat-model', 1),
  (1, 'q012-escape-模型<&"', 1),
  (2, 'q012-chat-model', 1),
  (2, 'q012-escape-模型<&"', 1),
  (3, 'q012-error-model', 1);

INSERT INTO requests
  (id, account_id, model_id, started_at, completed_at, status,
   input_tokens, output_tokens, cost_microdollars, upstream_latency_ms,
   error_message, protocol, streamed, exactness, cache_read_tokens,
   cache_write_tokens, reasoning_tokens, reserved_microdollars, first_byte_ms,
   retry_count, error_class, status_code, bytes_received, bytes_emitted,
   provider_id, original_model_id, first_attempt_at, upstream_protocol,
   cache_counter_status, input_tokens_reported, output_tokens_reported,
   total_tokens_reported, raw_usage_json)
VALUES
  (1, 1, 'q012-chat-model', datetime('now', '-2 hours'), datetime('now', '-1 hours', '-59 minutes'), 'completed',
   1200, 800, 123456, 420.0, NULL, 'openai', 0, 'provider_reported', 300, 40, 120, 150000, 190.0,
   0, NULL, 200, 48000, 12000, 'q012-alpha', 'q012-chat-model', datetime('now', '-2 hours'), 'openai', 'complete', 1200, 800, 2040, '{"input_tokens":1200,"output_tokens":800}'),
  (2, 2, 'q012-escape-模型<&"', datetime('now', '-3 hours'), datetime('now', '-2 hours', '-59 minutes'), 'completed',
   2400, 1600, 234567, 875.0, NULL, 'anthropic', 1, 'derived', 0, 180, 600, 250000, 350.0,
   0, NULL, 200, 96000, 22000, 'q012-alpha', 'q012-escape-模型<&"', datetime('now', '-3 hours'), 'anthropic', 'complete', 2400, 1600, 4180, '{"input_tokens":2400,"output_tokens":1600}'),
  (3, 3, 'q012-error-model', datetime('now', '-4 hours'), datetime('now', '-3 hours', '-59 minutes'), 'error',
   400, 0, 0, 210.0, 'Quota & <retries>', 'openai', 0, 'unknown', 0, 0, 0, 0, 0.0,
   1, 'rate_limit', 429, 12000, 1800, 'q012-beta', 'q012-error-model', datetime('now', '-4 hours'), 'openai', 'missing', 400, 0, 400, '{"error":"Quota & <retries>"}'),
  (4, 1, 'q012-chat-model', datetime('now', '-5 hours'), datetime('now', '-4 hours', '-59 minutes'), 'completed',
   3000, 2100, 345678, 1120.0, NULL, 'openai', 1, 'exact', 500, 90, 0, 400000, 520.0,
   1, NULL, 200, 130000, 31000, 'q012-alpha', 'q012-chat-model', datetime('now', '-5 hours'), 'openai', 'complete', 3000, 2100, 5690, '{"input_tokens":3000,"output_tokens":2100}'),
  (5, 3, 'q012-error-model', datetime('now', '-6 hours'), datetime('now', '-5 hours', '-59 minutes'), 'completed',
   800, 600, 456789, 640.0, NULL, 'openai', 0, 'estimated', 0, 0, 50, 500000, 280.0,
   0, NULL, 200, 36000, 9000, 'q012-beta', 'q012-error-model', datetime('now', '-6 hours'), 'openai', 'not_applicable', 800, 600, 1400, '{"input_tokens":800,"output_tokens":600}');

INSERT INTO reservations
  (id, request_id, account_id, model_id, reserved_microdollars, created_at,
   released_at, status, estimated_tokens, expires_at, release_reason)
VALUES
  (1, 4, 1, 'q012-chat-model', 400000, datetime('now', '-5 hours'), NULL, 'active', 6000, datetime('now', '+1 hour'), NULL);

INSERT INTO request_attempts
  (id, request_id, attempt_number, account_id, started_at, completed_at,
   status_code, error_class, upstream_request_id, provider_id, model_id,
   protocol, retry_category, release_reason, bytes_received, bytes_emitted,
   latency_ms, streamed, is_retry_outcome)
VALUES
  (1, 1, 1, 1, datetime('now', '-2 hours'), datetime('now', '-1 hours', '-59 minutes'), 200, NULL, 'q012-up-1', 'q012-alpha', 'q012-chat-model', 'openai', 'initial', NULL, 48000, 12000, 420, 0, 0),
  (2, 2, 1, 2, datetime('now', '-3 hours'), datetime('now', '-2 hours', '-59 minutes'), 200, NULL, 'q012-up-2', 'q012-alpha', 'q012-escape-模型<&"', 'anthropic', 'initial', NULL, 96000, 22000, 875, 1, 0),
  (3, 3, 1, 3, datetime('now', '-4 hours'), datetime('now', '-3 hours', '-59 minutes'), 429, 'rate_limit', 'q012-up-3', 'q012-beta', 'q012-error-model', 'openai', 'provider_error', 'backoff', 12000, 1800, 210, 0, 1),
  (4, 4, 1, 1, datetime('now', '-5 hours'), datetime('now', '-5 hours', '-59 minutes'), 503, 'upstream_error', 'q012-up-4a', 'q012-alpha', 'q012-chat-model', 'openai', 'failover', 'retry', 5000, 400, 130, 0, 1),
  (5, 4, 2, 1, datetime('now', '-5 hours'), datetime('now', '-4 hours', '-59 minutes'), 200, NULL, 'q012-up-4b', 'q012-alpha', 'q012-chat-model', 'openai', 'success', NULL, 130000, 31000, 1120, 1, 0),
  (6, 5, 1, 3, datetime('now', '-6 hours'), datetime('now', '-5 hours', '-59 minutes'), 200, NULL, 'q012-up-5', 'q012-beta', 'q012-error-model', 'openai', 'initial', NULL, 36000, 9000, 640, 0, 0);

INSERT INTO usage_rollups
  (bucket_start, bucket_size_s, provider_id, model_id, account_id, protocol,
   streamed, status, request_count, error_count, retry_count, input_tokens,
   output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens,
   cost_microdollars, bytes_received, bytes_emitted, latency_ms_sum,
   latency_ms_min, latency_ms_max, first_byte_ms_sum, first_byte_ms_count)
VALUES
  (strftime('%Y-%m-%d %H:00:00', datetime('now', '-2 hours')), 3600, 'q012-alpha', 'q012-chat-model', 1, 'openai', 0, 'completed', 1, 0, 0, 1200, 800, 300, 40, 120, 123456, 48000, 12000, 420, 420, 420, 0, 0),
  (strftime('%Y-%m-%d %H:00:00', datetime('now', '-3 hours')), 3600, 'q012-alpha', 'q012-escape-模型<&"', 2, 'anthropic', 1, 'completed', 1, 0, 0, 2400, 1600, 0, 180, 600, 234567, 96000, 22000, 875, 875, 875, 350, 1),
  (strftime('%Y-%m-%d %H:00:00', datetime('now', '-4 hours')), 3600, 'q012-beta', 'q012-error-model', 3, 'openai', 0, 'error', 1, 1, 1, 400, 0, 0, 0, 0, 0, 12000, 1800, 210, 210, 210, 0, 0),
  (strftime('%Y-%m-%d %H:00:00', datetime('now', '-5 hours')), 3600, 'q012-alpha', 'q012-chat-model', 1, 'openai', 1, 'completed', 1, 0, 1, 3000, 2100, 500, 90, 0, 345678, 130000, 31000, 1120, 1120, 1120, 520, 1),
  (strftime('%Y-%m-%d %H:00:00', datetime('now', '-6 hours')), 3600, 'q012-beta', 'q012-error-model', 3, 'openai', 0, 'completed', 1, 0, 0, 800, 600, 0, 0, 50, 456789, 36000, 9000, 640, 640, 640, 0, 0);

INSERT INTO account_events (id, account_id, event_type, details, created_at)
VALUES
  (1, 3, 'backoff', '{"reason":"Quota & <retries>","note":"unicode ✨"}', datetime('now', '-3 hours')),
  (2, 2, 'catalog_refresh', '{"models":2,"source":"fixture"}', datetime('now', '-2 hours'));

INSERT INTO provider_pings
  (id, provider_id, account_name, probed_at, latency_ms, status_code, error, model_count)
VALUES
  (1, 'q012-alpha', 'alpha-main', datetime('now', '-90 minutes'), 38, 200, NULL, 2),
  (2, 'q012-alpha', 'alpha-éclair', datetime('now', '-80 minutes'), 44, 200, NULL, 2),
  (3, 'q012-beta', 'beta-long-account-✨', datetime('now', '-70 minutes'), 920, 503, 'provider unavailable', 1);

INSERT INTO routing_decisions
  (id, request_id, attempt_number, model_id, provider_id,
   selected_account_id, selected_account_name, selected_tier, selected_score,
   eligible_count, scored_count, attempted_excluded_count, top_score,
   top_score_account_name, exclude_reasons_json, decision_made_at)
VALUES
  (1, 1, 1, 'q012-chat-model', 'q012-alpha', 1, 'alpha-main', 1, 0.91, 2, 2, 0, 0.91, 'alpha-main', '[]', datetime('now', '-2 hours')),
  (2, 2, 1, 'q012-escape-模型<&"', 'q012-alpha', 2, 'alpha-éclair', 1, 0.82, 2, 2, 0, 0.82, 'alpha-éclair', '[]', datetime('now', '-3 hours')),
  (3, 3, 1, 'q012-error-model', 'q012-beta', 3, 'beta-long-account-✨', 1, 0.30, 1, 1, 0, 0.30, 'beta-long-account-✨', '[]', datetime('now', '-4 hours')),
  (4, 4, 1, 'q012-chat-model', 'q012-alpha', 1, 'alpha-main', 1, 0.77, 2, 2, 0, 0.77, 'alpha-main', '[]', datetime('now', '-5 hours')),
  (5, 5, 1, 'q012-error-model', 'q012-beta', 3, 'beta-long-account-✨', 1, 0.42, 1, 1, 0, 0.42, 'beta-long-account-✨', '[]', datetime('now', '-6 hours'));

INSERT INTO model_price_snapshots
  (model_id, input_price_per_1k, output_price_per_1k, captured_at,
   input_per_million_microdollars, output_per_million_microdollars,
   source, metadata_json, provider_id)
VALUES
  ('q012-chat-model', 0.10, 0.20, datetime('now', '-1 day'), 100000, 200000, 'fixture', '{}', 'q012-alpha'),
  ('q012-escape-模型<&"', 0.12, 0.24, datetime('now', '-1 day'), 120000, 240000, 'fixture', '{}', 'q012-alpha');
