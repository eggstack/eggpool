-- Indexes for EggPool

CREATE INDEX IF NOT EXISTS idx_accounts_name ON accounts(name);
CREATE INDEX IF NOT EXISTS idx_accounts_enabled ON accounts(enabled);

CREATE INDEX IF NOT EXISTS idx_account_models_account ON account_models(account_id);
CREATE INDEX IF NOT EXISTS idx_account_models_model ON account_models(model_id);

CREATE INDEX IF NOT EXISTS idx_requests_account ON requests(account_id);
CREATE INDEX IF NOT EXISTS idx_requests_model ON requests(model_id);
CREATE INDEX IF NOT EXISTS idx_requests_started ON requests(started_at);
CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);

CREATE INDEX IF NOT EXISTS idx_reservations_account ON reservations(account_id);
CREATE INDEX IF NOT EXISTS idx_reservations_model ON reservations(model_id);
CREATE INDEX IF NOT EXISTS idx_reservations_status ON reservations(status);
CREATE INDEX IF NOT EXISTS idx_reservations_request ON reservations(request_id);

CREATE INDEX IF NOT EXISTS idx_model_price_snapshots_model ON model_price_snapshots(model_id);
CREATE INDEX IF NOT EXISTS idx_model_price_snapshots_captured ON model_price_snapshots(captured_at);

CREATE INDEX IF NOT EXISTS idx_account_events_account ON account_events(account_id);
CREATE INDEX IF NOT EXISTS idx_account_events_type ON account_events(event_type);
CREATE INDEX IF NOT EXISTS idx_account_events_created ON account_events(created_at);
