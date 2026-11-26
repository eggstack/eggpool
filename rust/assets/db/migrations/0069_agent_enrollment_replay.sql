CREATE TABLE consumed_agent_enrollment_tickets (
    ticket_sha256 BLOB PRIMARY KEY NOT NULL
        CHECK(typeof(ticket_sha256) = 'blob' AND length(ticket_sha256) = 32),
    expires_at INTEGER NOT NULL CHECK(expires_at > 0)
);

CREATE INDEX consumed_agent_enrollment_tickets_expiry
    ON consumed_agent_enrollment_tickets(expires_at);
