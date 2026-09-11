-- Deprecate withdrawn models: relink historical request/reservation
-- rows to a single shared placeholder so the original model row can
-- be deleted, while still preserving the model id in stats queries.

INSERT OR IGNORE INTO models (
    model_id, display_name, protocol, resolution_status, provider_id
) VALUES (
    '__deprecated__', 'Deprecated models', 'openai', 'resolved', 'opencode-go'
);

ALTER TABLE requests ADD COLUMN original_model_id TEXT;

CREATE INDEX IF NOT EXISTS idx_requests_original_model_id
    ON requests(original_model_id) WHERE original_model_id IS NOT NULL;

ALTER TABLE reservations ADD COLUMN original_model_id TEXT;

CREATE INDEX IF NOT EXISTS idx_reservations_original_model_id
    ON reservations(original_model_id) WHERE original_model_id IS NOT NULL;
