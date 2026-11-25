# Provider Agents Milestone 002 — One-Time Enrollment and Owner Attach

Status: implemented

Repository baseline: `c4d774aeb883307f28ef94f6143dc688b3098946`

Source roadmap:

- `plans/subsystems/provider-agents-roadmap.md#7-milestones`

Long-term requirements:

- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/000-long-term-specification.md#3-ownership-boundaries-normative`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`

Applicable ADRs:

- `plans/adrs/ADR-0016-provider-agent-upstreams.md` (accepted)
- `plans/adrs/ADR-0010-personal-upstream-account-custody.md` (binding and atomic publication model)

Primary class: capability

## 1. Objective

Let a signed-in managed user paste one `eggpass eggpool` ticket and attach that
agent to their own EggPool account, with a revocable per-agent transport
credential and no provider token ever reaching the server.

## 2. Why this milestone is ready

Implemented and conditionally closed.

- Hard: satisfied. ADR-0016 acceptance is satisfied (2026-11-24), and M001 is
  implemented and conditionally closed through
  `plans/closure/provider-agents/001-status.md`; the agent account kind, the
  tunnel manager, and the owner gate exist and are verified.
- Interface: the ticket format in `eggstack/eggpass#10` is agreed and
  implemented in-repository as a fixture producer, which exercises the whole
  path including a real reverse client authenticating with the adopted
  credential. The live producer does not exist yet, so the milestone is
  conditionally closed: the capability is complete and verified, and the named
  outstanding condition is the live producer. See
  `plans/closure/provider-agents/002-status.md`.
- Hard: the repository-wide dependency-audit gate and the live ticket producer
  are both external to this milestone's implementation; neither is an M002
  interface defect.

## 3. Current implementation evidence

- `rust/src/server/oauth_login.rs` is the closest existing pattern: a bounded,
  session-bound, memory-only pending-flow registry with CSRF on every mutation,
  permission re-checks, and a commit that delegates to an owner-bound store.
- `rust/src/server/oauth_credentials.rs` shows the self-only bearer route with a
  single scope, owner derived only from `AuthenticatedPrincipal.user_id`, and
  typed error envelopes.
- `rust/src/personal_upstream.rs` owns `personal_upstream_bindings` and the
  grant/eligibility snapshot; `rust/src/operations/config_mutation.rs` owns
  atomic config publication with rollback through
  `install_personal_account_files`/`omit_personal_account_files`.
- `rust/src/security_events.rs` records typed security events without secrets.
- `rust/src/server/mod.rs` registers the member routes; `middleware.rs` owns the
  route-role gate; `web_session.rs` owns CSRF and session fencing.
- Eggpass ticket production does not exist yet (`eggstack/eggpass#10`).

## 4. Invariants that must not regress

- The owner is derived only from the verified principal; no request field selects
  a user, provider owner, or binding.
- A ticket is single-use, short-lived, bounded, and never logged, persisted, or
  returned in diagnostics or status.
- No provider access token, refresh token, ID token, or PKCE material is accepted
  or stored.
- Account and binding publication is atomic; a failure leaves no partial config,
  no binding, and no listener.
- The operator gate, effective grant, and active binding are checked before
  enrollment and before the account becomes routable.
- CSRF is required on every mutation; the enrollment route is not an exception
  to the authenticated browser boundary.
- The reverse-server external listener stays loopback-only.

## 5. Scope

### In scope

- Ticket decoding and validation for `epagent1.<base64url(json)>`.
- A bounded, memory-only pending-enrollment registry with one-time consumption.
- Authenticated Web UI enrollment and revocation routes with CSRF.
- Per-agent transport credential issuance and storage.
- Atomic account configuration, binding creation, and tunnel start with rollback.
- Security events and bounded, secret-free errors.
- A test ticket producer and tamper/replay matrix.

### Explicitly out of scope

- Eggpass-side command implementation (upstream).
- Model origin labels and catalog discovery (M003).
- Removing OAuth, `[oauth]`, or any existing login surface (M004).
- Bulk operator enrollment or CLI enrollment.
- A hosted relay or public callback.

## 6. Required production changes

**Ticket module (new, likely `rust/src/agents/ticket.rs`).** Decode
`epagent1.<base64url(json)>` with a hard input cap (≤ 2 KiB), a version field,
required `control`, `server_name`, CA pin, `enroll`, `expires_at`, optional
`agent` label and provider list. Reject unknown versions, malformed JSON,
oversized input, expired tickets, missing fields, control-address syntax errors,
and a CA pin that does not match the configured agent CA. Zeroize the token after
use. The module returns typed errors that never echo ticket content.

**Pending registry (new, `rust/src/agents/enrollment.rs`).** Bounded (for
example 64 entries), TTL-bounded, keyed by a hash of the ticket token, holding
the initiating session and user. Consuming a ticket removes it atomically; a
second use fails. The registry is memory-only; a restart invalidates pending
tickets, which is acceptable and documented.

**Routes (`rust/src/server/`).** `POST /my-account/agents/enroll` and
`POST /my-account/agents/revoke`, CSRF-protected, member-accessible. Enrollment
checks the operator agent gate, the effective grant, and account-name validity;
it derives the user from the session. Revocation removes the account's transport
credential, stops its tunnel, and either removes or disables the account and
binding per the chosen policy. Both emit typed security events.

**Credential issuance.** v1 issues a random per-agent transport secret (32 bytes,
base64url) stored in the config-adjacent `.env` as
`EGGPOOL_AGENT_<hash>`, mode `0600`, never in SQLite or the TOML. A decision
point in work package C upgrades this to a client certificate signed by a
per-install agent CA; only proceed if the CA key, file permissions, rotation, and
backup behavior can be bounded in this milestone.

**Atomic publication.** Reserve the external port, start the reverse server,
then publish the account configuration and binding through the existing mutation
path. On any failure, stop the server, remove the staged secret, and leave the
previous configuration intact. A concurrent enrollment for the same account name
loses cleanly.

**Gate and revocation.** A new configuration gate (for example `[agents].enabled`,
default on) blocks enrollment and routing while retaining credentials, mirroring
`[oauth].enabled`. Revocation must drop the live control connection.

## 7. Ordered work packages

### Work package A — Ticket format and decoder

Intent: one canonical parser for both production and tests.

Required changes: module, typed errors, bounds, versioning, zeroization; a test
producer that mints valid tickets with a local CA pin.

Acceptance evidence: unit matrix for prefix, base64, JSON, version, size,
expiry, missing fields, control syntax, CA mismatch; no ticket content in error
text.

### Work package B — Pending registry and routes

Intent: make attachment a bounded, CSRF-protected browser action.

Required changes: registry, handlers, redirects, error envelopes, route
registration, middleware role checks.

Acceptance evidence: replay, expiry, concurrent consume, CSRF failure,
non-member denial, disabled-gate denial, session change cancellation.

### Work package C — Credential issuance

Intent: give each agent a revocable transport credential.

Required changes: secret generation/storage; the mTLS decision and, if taken,
CA key generation, client-cert issuance, file permissions, and rotation; config
wiring into the reverse server.

Acceptance evidence: secret is `0600`, absent from logs/status/backups as
plaintext beyond the documented file, and revocable; mTLS (if selected) proves a
wrong-client-cert rejection.

### Work package D — Atomic attach and rollback

Intent: never leave a half-attached account.

Required changes: port reservation, tunnel start, config/binding publication,
rollback ordering, idempotence on retry.

Acceptance evidence: injected failure at each step leaves no account, binding,
secret, or listener; a successful attach routes a fixture request.

### Work package E — Revocation

Intent: immediate local disconnect and cleanup.

Required changes: stop tunnel, drop credential, remove or disable the account
and binding, audit event, UI state.

Acceptance evidence: the control connection closes, subsequent requests fail
closed, and re-enrollment requires a fresh ticket.

## 8. Failure, cancellation, restart, contention semantics

- A malformed or expired ticket is rejected without consuming the registry entry
  for a different token.
- A consumed ticket cannot be replayed; a second request fails with a typed
  conflict.
- Enrollment is atomic: listener, secret, config, and binding either all exist or
  none do.
- A restart invalidates pending tickets but preserves enrolled accounts and
  credentials.
- Concurrent enrollments for one account are serialized; the loser gets a
  conflict, not a partial state.
- Revocation during an in-flight request closes the tunnel after a bounded grace
  period; the in-flight request fails closed.

## 9. Compatibility and migration

No storage migration. The new gate and account kind are additive. Existing OAuth
accounts and routes are untouched. Backup must include the agent credential file
and exclude the ticket; the format change is deferred to M004.

## 10. Required tests

- `rust/tests/agent_enrollment.rs`: decode/tamper matrix, replay, expiry,
  oversized input, unknown version, CA mismatch, no secret echo.
- `rust/tests/on_prem_admin_ui.rs`: CSRF, member/non-member, disabled gate,
  redirect and error surfaces, revocation.
- `rust/tests/agent_upstreams.rs`: attach → fixture request; revoke → fail closed.
- `rust/tests/provider_oauth.rs` or a new guard: OAuth routes and behavior are
  unchanged.
- Config mutation rollback tests; security-event assertions; default and
  no-default serial suites.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test agent_enrollment -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test agent_upstreams -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_admin_ui -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo build --manifest-path rust/Cargo.toml --locked --release
```

## 12. Documentation updates

- `docs/providers.md`: the enrollment journey and the one-time ticket.
- `docs/backup-restore.md`: agent credential inclusion and ticket exclusion.
- `architecture/deep-dive-providers.md`: enrollment, credential custody, and
  revocation ownership.
- `AGENTS.md`: `rust/src/agents/` ownership and secret rules.
- The upstream ticket format in `eggstack/eggpass#10` if it differs from this
  plan.

## 13. Acceptance criteria

- A valid ticket attaches the agent to the signed-in user, and a subsequent
  request routes through the tunnel.
- Every tamper, replay, expiry, CSRF, non-owner, and disabled-gate case fails
  closed with a typed, secret-free error.
- A failed attach leaves no partial state.
- Revocation closes the tunnel and removes the credential.
- No ticket, token, or provider credential appears in logs, status, diagnostics,
  SQLite, or the TOML.

## 14. Stop conditions

Stop and report if: the ticket format cannot be agreed upstream; the milestone
would need the ticket to carry a provider token; the CA work cannot be bounded
when mTLS is attempted; atomic config/binding publication with rollback cannot be
guaranteed; or the route would need to trust Host/Forwarded values or a
caller-selected user.

## 15. Closure evidence required

- Decode/tamper/replay/expiry matrix output.
- CSRF, role, gate, and session-change evidence.
- Atomic rollback evidence for every failure point.
- Revocation and post-revocation fail-closed evidence.
- Secret-storage permissions and redaction evidence.
- Default and no-default serial suite results; strict Clippy; locked build.

## 16. Handoff notes

Treat the ticket as a hostile input. Never log it. Keep the pending registry
memory-only and bounded. Do not reuse the OAuth flow registry or its error
vocabulary; the lifecycles differ. If the upstream format changes, update the
decoder and the issue together. Rust tests run serially.
