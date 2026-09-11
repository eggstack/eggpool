-- D001 seed rows applied after migrations 0001..0054.
-- This file contains semantic state only; credentials and proxy secrets are
-- intentionally absent. Tests apply it to a freshly migrated copy.

INSERT INTO providers (id, provider_id, base_url, protocols, enabled)
VALUES
    (10, 'provider-a', 'https://provider-a.invalid/v1', '["openai","anthropic"]', 1),
    (11, 'provider-b', 'https://provider-b.invalid/v1', '["openai"]', 1);

INSERT INTO accounts (id, name, api_key_env, enabled, weight, provider_id)
VALUES
    (1, 'account-a', 'D001_ACCOUNT_A_KEY', 1, 2.0, 'provider-a'),
    (2, 'account-b', 'D001_ACCOUNT_B_KEY', 1, 1.0, 'provider-b'),
    (3, 'disabled-a', 'D001_DISABLED_KEY', 0, 1.0, 'provider-a');

INSERT INTO models (
    model_id, display_name, protocol, capabilities, source_metadata,
    protocol_source, resolution_status, provider_id
)
VALUES
    ('shared-model', 'Shared Model', 'openai', '{"supports_tools":true}',
     '{"fixture":"d001"}', 'config', 'resolved', 'provider-a'),
    ('withdrawn-model', 'Withdrawn Model', 'openai', '{}',
     '{"fixture":"d001"}', 'config', 'resolved', 'provider-a');

INSERT INTO account_models (account_id, model_id, enabled)
VALUES
    (1, 'shared-model', 1),
    (2, 'shared-model', 1),
    (1, 'withdrawn-model', 1);

INSERT INTO provider_model_metadata (
    model_id, provider_id, display_name, protocol, capabilities,
    source_metadata, protocol_source, resolution_status
)
VALUES
    ('shared-model', 'provider-a', 'Shared Model', 'openai',
     '{"supports_tools":true}', '{"fixture":"d001"}', 'config', 'resolved'),
    ('shared-model', 'provider-b', 'Shared Model', 'anthropic',
     '{"supports_tools":true}', '{"fixture":"d001"}', 'config', 'resolved'),
    ('withdrawn-model', 'provider-a', 'Withdrawn Model', 'openai', '{}',
     '{"fixture":"d001"}', 'config', 'resolved');

INSERT INTO catalog_refresh_state (
    account_id, provider_id, last_successful_refresh_at, last_outcome, model_count
)
VALUES
    (1, 'provider-a', '2026-09-04 00:00:00', 'success_authoritative', 2),
    (2, 'provider-b', '2026-09-04 00:00:00', 'success_authoritative', 1);

INSERT INTO requests (
    id, account_id, model_id, provider_id, protocol, status,
    input_tokens, output_tokens, cost_microdollars, streamed
)
VALUES
    (1, 1, 'shared-model', 'provider-a', 'openai', 'success', 100, 50, 700, 0),
    (2, 2, 'shared-model', 'provider-b', 'anthropic', 'pending', 20, 10, 100, 1);

INSERT INTO reservations (
    id, request_id, account_id, model_id, reserved_microdollars, status
)
VALUES
    (1, 1, 1, 'shared-model', 700, 'released'),
    (2, 2, 2, 'shared-model', 500, 'active');

INSERT INTO account_backoffs (
    account_id, model_id, reason, status_code, error_class,
    consecutive_failures, backoff_until, last_failure_at, updated_at
)
VALUES
    (1, NULL, 'rate_limited', 429, 'rate_limited', 2,
     '2026-09-04 00:10:00', '2026-09-04 00:00:00', '2026-09-04 00:00:00'),
    (1, 'withdrawn-model', 'model_unavailable', 404, 'model_not_found', 1,
     '2026-09-04 00:05:00', '2026-09-04 00:00:00', '2026-09-04 00:00:00');

INSERT INTO model_quarantine (
    provider_id, account_id, canonical_model_id, upstream_model_id,
    upstream_protocol, state, evidence_provenance, reason,
    first_observed, last_observed, observation_count, expiry,
    last_status_code, last_error_class
)
VALUES
    ('provider-a', 'account-a', 'withdrawn-model', 'withdrawn-model',
     'openai', 'quarantined', 'runtime_http', 'model_unavailable',
     '2026-09-04 00:00:00', '2026-09-04 00:01:00', 2,
     '2026-09-04 00:06:00', 404, 'model_not_found'),
    ('provider-b', 'account-b', 'shared-model', NULL,
     'anthropic', 'suspected', 'provider_catalog', 'partial_refresh',
     '2026-09-04 00:00:00', '2026-09-04 00:00:00', 1,
     '2026-09-04 00:02:00', NULL, NULL);
