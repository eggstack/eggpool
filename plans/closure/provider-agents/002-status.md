# Provider Agents Milestone 002 — Closure Status

Status: conditionally closed

Source implementation plan:

- `plans/implementation/provider-agents/002-owner-enrollment-and-revocation.md`

Source subsystem roadmap:

- `plans/subsystems/provider-agents-roadmap.md#7-milestones`

Repository baseline reviewed: `ef61fe0e` (Conditionally close the provider agent
tunnel foundation milestone)

Implementation commits or pull requests:

- `802b0878` — Add the one-time agent enrollment ticket and owner attach and
  revocation

## 1. Executive finding

The M002 capability boundary is complete and verified. A signed-in managed
member submits an `eggpass eggpool` ticket to `POST /my-account/agents/enroll`;
EggPool adopts the ticket's `enroll` value as the account's single transport
credential, stages the credential and the account configuration, inserts the
owner binding, publishes both files atomically, and starts the agent's reverse
tunnel. A fixture inference request is then served through the enrolled
account, and a reverse client presenting the WRONG credential is refused. The
routes are CSRF-protected and member-only; an administrator-role session is
denied. Revocation removes the account, the owner binding, and the credential,
stops the tunnel, and is idempotent.

The milestone is **conditionally closed**, not closed, for one named reason:
the live ticket producer in `eggstack/eggpass#10` does not exist. The
in-repository fixture producer exercises the entire path, including a real
Eggress reverse client authenticating with the adopted credential, so the
capability is proven; the user-facing journey cannot close until the real
producer ships. See §10.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Ticket format `epagent1.<base64url(json)>` decodes | `rust/src/agents/ticket.rs` decoder | pass | Version field required; unknown version rejected |
| Hard input cap on the decoded ticket | `rust/src/agents/ticket.rs` decode path | pass | 2 KiB pre-decode cap; oversized input rejected |
| Malformed JSON, missing fields, control-address syntax rejected | `rust/tests/agent_enrollment.rs` ticket producer matrix | pass | Typed errors; no ticket content in any error text |
| Expired ticket rejected | same matrix | pass | Expiry compared against wall clock |
| CA pin must match the configured agent CA | `rust/src/config.rs` `AgentTlsConfig.server_ca_path`; ticket validation | pass | The ticket carries the authority for EggPool's agent server certificate |
| Ticket secret is zeroized | `rust/src/agents/ticket.rs` `Zeroizing` payload and secret | pass | Decoded buffer, decoded payload, and retained secret are zeroized |
| Ticket `Debug` reveals no field value | `rust/src/agents/ticket.rs` `Debug` impl | pass | Every field renders as `[REDACTED]` |
| Registry is bounded, TTL-bounded, memory-only | `rust/src/agents/enrollment.rs` | pass | 64 entries; 2 minute maximum TTL; SHA-256 digest keyed |
| A ticket is single-use | `rust/src/agents/enrollment.rs` reserve/commit | pass | Consumption is committed only after a successful attach |
| A FAILED enrollment does not burn the ticket | reservation released on every rollback path | pass | The same ticket can be retried after a failure |
| A concurrent second use fails while the first is in flight | reservation is held for the initiating session | pass | The in-flight reservation is visible to a second attempt |
| Enrollment route is CSRF-protected | `rust/src/server/agents.rs` `csrf_matches` | pass | Both mutations; denial is typed and secret-free |
| Enrollment route is member-only | `rust/src/server/agents.rs` `self_service_role_error` | pass | An administrator-role session is refused |
| Owner comes only from the verified principal | `rust/src/server/agents.rs` enrollment handler | pass | No request field selects a user, provider owner, or binding |
| Operator gate and effective grant checked before enrollment | enrollment handler | pass | `[agents].enabled` and the self-service grant |
| Publication is atomic; a failure leaves no partial state | `rust/src/operations/config_mutation.rs` prepare/publish split | pass | Files are STAGED, the binding is inserted, then both are published atomically |
| A crash before publication strands no binding | `rust/src/personal_upstream.rs` `reconcile_agent_bindings` | pass | Startup removes bindings whose agent account is absent from the published config |
| An operator can clear a stranded binding and credential | revocation route; `reconcile_agent_bindings` | pass | Revocation works for an account that was never published |
| A port collision does not burn the ticket | `rust/src/server/agents.rs` bounded fresh-port retry | pass | Bounded retries before ticket consumption is committed |
| Revocation removes account, binding, and credential; idempotent | `rust/src/server/agents.rs` revoke handler | pass | Repeating revocation after removal is a no-op |
| No credential in SQLite | `rust/tests/agent_enrollment.rs` whole-database dump assertion | pass | Every value of every table is scanned, not one projection |
| No credential in the TOML | same test | pass | The TOML carries the environment-variable NAME only |
| No ticket, secret, CA material, or credential in logs, status, diagnostics, or responses | `rust/src/security_events.rs`; typed error envelopes | pass | Events carry provider/account identifiers and a generic reason only |
| No provider access, refresh, or ID token and no PKCE material accepted or stored | ticket schema; `rust/src/agents/ticket.rs` | pass | The ticket carries no provider credential at all |
| Credential file mode | `rust/src/operations/config_mutation.rs` env publication | pass | `0600`; the secret lives in the config-adjacent `.env` |
| OAuth and static-key paths behaviourally unchanged | no OAuth branch modified | pass | Direct OAuth custody stays in force until M004 |
| Reload classification for the new configuration keys | `rust/src/config_reload_policy.rs` | pass | Agent credential and listener keys are restart-required |
| Backup includes the agent credential and excludes the ticket | `rust/src/operations/backup.rs` | pass | The credential rides in the existing `.env` member; the ticket is never persisted |
| `--no-default-features` parity | full suite in both feature configurations | pass | No SSH-dependent code in this milestone |
| End-to-end request through an enrolled account | `rust/tests/agent_enrollment.rs` `enrolled_agent_routes_inference_through_reverse_client` | pass | A real `eggress_protocol_reverse::client::ReverseClient` stands in for Eggpass |
| A wrong credential is refused | `rust/tests/agent_enrollment.rs` `wrong_enrollment_credential_cannot_dispatch_provider_request` | pass | No provider request reaches the agent |
| No fixed sleeps as readiness proofs | `rust/tests/agent_enrollment.rs` `wait_for_control_count` | pass | Bounded deadline on the observed reverse-server control counter |

## 3. Production implementation evidence

Landed ownership:

- `rust/src/agents/ticket.rs` — the ticket decoder: `epagent1.<base64url(json)>`,
  the 2 KiB pre-decode cap, the version field, the expiry, the control-address
  range check against the operator-assigned port range, the CA-pin check, the
  zeroizing secret, and a `Debug` impl that renders no field value.
- `rust/src/agents/enrollment.rs` — `PendingEnrollmentRegistry`: bounded at 64
  entries, TTL-bounded at 2 minutes, memory-only, keyed by a SHA-256 digest of
  the enrollment secret. `reserve` takes a reservation; `commit` consumes it
  permanently; dropping an uncommitted reservation releases it so the same
  ticket can be retried; `retain_until_expiry` holds an ambiguous result until
  the ticket expires.
- `rust/src/server/agents.rs` — `POST /my-account/agents/enroll` and
  `POST /my-account/agents/revoke`: CSRF, the member role gate, the
  operator gate, the effective grant, the bounded fresh-port retry on a bind
  collision, and the typed secret-free error envelopes.
- `rust/src/agents/tunnel.rs` — `AgentTunnelManager::start_account` for the
  enrolled account's tunnel.
- `rust/src/operations/config_mutation.rs` — the prepare/publish split:
  `prepare_personal_agent_account_files` stages the credential and the account
  configuration and captures the file modes before any write;
  `PreparedPersonalAgentAccountFiles::publish` writes the credential, validates
  the candidate configuration, writes the configuration, and restores the
  credential on either failure.
- `rust/src/personal_upstream.rs` — owner-binding publication and
  `reconcile_agent_bindings`, the startup reconciliation that removes a binding
  whose agent account is absent from the published configuration and clears the
  credential published just before a crash.
- `rust/src/config.rs`, `rust/src/config_reload_policy.rs` — the agent server CA
  authority and the restart-required classification of the new keys.
- `rust/src/security_events.rs` — the typed, secret-free enrollment and
  revocation events.
- `rust/src/server/mod.rs`, `rust/src/server/middleware.rs` — the two member
  routes and the route role gate.

Two deliberate deviations from the plan, both recorded here:

1. The plan's work package C says EggPool issues a random per-agent transport
   secret. The implementation ADOPTS the ticket's `enroll` value as the
   transport credential instead. The reason is structural: the ticket has no
   return channel, so a credential minted by EggPool cannot be delivered to the
   agent. The ticket's secret IS the credential. EggPool still stores it only
   in the config-adjacent `.env` with mode `0600`, and the TOML carries the
   environment-variable NAME only.
2. `agent.control_bind` remains EggPool's own local listen socket, and the
   ticket's `control` field is the address the AGENT dials. The control port
   is operator-assigned, per ADR-0016.

Dependency change: none. No package was added, removed, or re-pinned; the
Eggfetch `0.4.0`, Eggress `1.0.12`, and EggServe `0.4.0` pins are unchanged
and no advisory waiver was added.

## 4. Verification executed

### Commands run

All commands below were run LOCALLY on 2026-11-24 in the authorized worktree.
No commit was pushed and no hosted CI run was executed for this change, so no
CI evidence is claimed.

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test agent_enrollment -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test agent_upstreams -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib -- --test-threads=1 agents::
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo deny --manifest-path rust/Cargo.toml check
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run python scripts/validate_release_docs.py
git diff --check
```

### Results

| Check | Result |
|---|---|
| `cargo fmt --check` | pass |
| strict Clippy, default features | pass, zero warnings |
| `cargo check --no-default-features` | pass |
| strict Clippy, `--no-default-features` | pass, zero warnings |
| `agent_enrollment` | 13 passed, 0 failed |
| `agent_upstreams` | 2 passed, 0 failed |
| `agents::` unit tests | 9 passed, 0 failed (347 filtered out) |
| full serial workspace suite, default features | 1387 passed, 0 failed, 2 ignored (98 result blocks) |
| full serial workspace suite, `--no-default-features` | 1388 passed, 0 failed, 2 ignored (98 result blocks) |
| `cargo build --locked --release` | pass |
| `cargo deny check` | **advisories FAILED**; bans ok, licenses ok, sources ok — see §10 |
| tooling suite | 267 passed, 1 skipped |
| `scripts/validate_release_docs.py` | pass |
| `git diff --check` | clean |

The `cargo deny` result is a genuine failure of the advisory gate and is not
reported as a pass. Its scope is established in §10.

### Independent review

An independent verifier audited the uncommitted implementation before closure.
It returned six MEDIUM, two LOW, and one verification-gap finding — eleven
items in all. Every one was fixed and re-verified by the matrix above:

- MEDIUM — a crash between the binding insert and the publication stranded a
  binding and a credential, and startup recovery did not reconcile agent
  bindings. Publication is now staged-then-bound-then-published, and
  `reconcile_agent_bindings` removes a binding whose agent account is absent
  from the published configuration.
- MEDIUM — a file-mode error bypassed the `.env` rollback, leaving a stranded
  credential. The file modes are now captured during the prepare step, before
  any write.
- MEDIUM — the ticket was consumed before port allocation and startup, so a
  failure burned it. Consumption is now reserved before and committed only
  after a successful attach.
- MEDIUM — the external-port probe/release race failed enrollment. A bind
  collision now retries with a fresh port a bounded number of times, before
  ticket consumption is committed.
- MEDIUM — replay protection was process-local while the API implied a
  stronger guarantee. The guarantee was narrowed explicitly in the code, in
  `docs/providers.md`, and in the registry's documentation: it holds for the
  lifetime of the EggPool process, and a restart inside the ticket's validity
  window can permit reuse. The ticket's maximum validity is two minutes, and
  the registry TTL is bounded to the same window.
- MEDIUM — an administrator-role session could enroll. The role boundary is
  now enforced explicitly and a test proves the denial.
- MEDIUM (verification gap) — the ticket-adopted credential was never proven
  on the agent-side control handshake. A real `ReverseClient` now enrolls from
  a fixture ticket, becomes an active control connection, serves a fixture
  inference request through the enrolled account, and is refused with a wrong
  credential.
- LOW — the decoded enrollment secret was not zeroized. It is now wrapped in
  `Zeroizing`.
- LOW — the ticket `Debug` exposed ticket metadata. Every field now renders
  as `[REDACTED]`.
- LOW — the SQLite "secret absent" assertion scanned one projection of one
  table and could not detect a credential stored elsewhere. It now scans every
  value of every table in the database.
- TEST QUALITY — the CSRF-denial, admin-boundary, session-change,
  retry-after-failure, and credential-rotation cases are now covered by tests.

## 5. Invariant review

- **No provider token on the server.** The ticket carries no provider
  credential at all. The adopted value is the agent's own local credential,
  used both as the account bearer credential and as the reverse control-channel
  password. No access token, refresh token, ID token, or PKCE material is
  read, stored, or logged. Direct OAuth custody is untouched by this
  milestone.
- **Owner-only selection, no pooling.** The owner is taken from the verified
  principal only. M001's owner gate still admits an agent account only for its
  bound owner, and M002 adds no path that crosses an owner boundary.
- **The ticket is hostile input.** It is size-capped, version-checked,
  expiry-checked, syntax-checked, CA-checked, zeroized, and never logged,
  persisted, or returned in a diagnostic.
- **No credential in persistence.** The secret lives only in the
  config-adjacent `.env`; the TOML carries the variable name; SQLite carries
  no credential, which a whole-database assertion proves.
- **No partial state.** A failed enrollment leaves no account, no binding, no
  credential, and no listener; a crash before publication leaves no binding at
  all, and startup reconciliation clears what a crash could strand.
- **Reload authority unchanged.** `classify_transition` remains the only
  reload-versus-restart authority; no independent key list was introduced.
- **OAuth behaviour unchanged.** No OAuth branch was modified. The OAuth
  routes, the OAuth credential store, the `[oauth]` configuration, and the
  `oauth-login` CLI command all remain fully in force until M004.
- **Performance boundaries unchanged.** The single SQLite gate, the streaming
  mpsc bridge, the `current_thread` Tokio runtime, and the routing selection
  lock are untouched. The enrollment registry is memory-only and adds no
  SQLite connection.

## 6. Failure and recovery review

- **Failed attach.** The reservation is released, the staged credential is
  removed, the tunnel is closed, and no binding or account survives. The same
  ticket can be retried.
- **Port collision.** The bind retries with a fresh port a bounded number of
  times before ticket consumption is committed, so a collision is an
  availability failure rather than a ticket failure.
- **Crash before publication.** No binding was inserted, so no binding is
  stranded; at worst an unreferenced staged credential file remains, and
  startup reconciliation removes it.
- **Stranded binding or credential.** Startup reconciliation removes a binding
  whose agent account is absent from the published configuration, and the
  revocation route clears a stranded binding and credential for an account
  that was never published.
- **Concurrent enrollment of one account name.** The second attempt loses
  cleanly with a typed conflict; it does not receive a partial state.
- **Registry capacity or expiry.** A bounded registry returns a typed capacity
  error; an expired entry is dropped and its ticket becomes unusable rather
  than silently reusable.
- **Revocation during an in-flight request.** The tunnel closes and the
  credential is dropped; subsequent requests fail closed. The in-flight
  request fails at the transport, and no other account is substituted.
- **Ambiguous enrollment outcome.** A result that cannot be attributed is
  retained until the ticket expires rather than released, so a retry cannot
  race an enrollment that may have succeeded.

## 7. Migration and compatibility review

- Existing configurations load unchanged. The new keys are additive, the
  account kind is unchanged, and a configuration with no agent accounts loads
  and round-trips as before.
- No storage migration. No schema change. The persisted model, quota, health,
  and binding tables are untouched.
- `[oauth]`, `AccountAuthMode::OAuth`, the OAuth credential store, the OAuth
  routes, the `oauth-login` CLI command, and the ADR-0015 refresh path all
  remain fully in force. Direct OAuth custody is retired only in Provider
  Agents M004.
- Backup behaviour is unchanged: the agent credential rides in the existing
  `.env` archive member, the ticket is never persisted and therefore cannot
  appear in an archive, and the archive format is untouched. The format
  change for the OAuth credential directory is deferred to M004, as the plan
  requires.

## 8. Security review

- **Transport.** The control channel is TLS with operator-supplied material;
  the ticket's CA pin is checked against the configured agent server CA before
  anything is published.
- **Authentication.** The reverse server authenticates the agent with the
  adopted per-agent credential, which the ticket's holder already possesses.
- **Secret hygiene.** No enrollment secret, ticket content, CA material, or
  credential appears in a `Debug`, an error string, a status value, a
  diagnostic, or an HTTP response body. The ticket `Debug` renders no field
  value at all.
- **Privilege bounds.** The member role gate is explicit; an
  administrator-role session is refused with a typed error. The owner comes
  only from the verified principal. The operator gate `[agents].enabled`
  blocks enrollment and routing.
- **CSRF.** Both mutations require the browser CSRF token and the same-origin
  check.
- **Audit.** Enrollment and revocation emit typed security events that carry
  provider and account identifiers and a generic reason only.
- **One-time honesty.** The API, the documentation, and the tests now state
  the guarantee that is actually provided — process-lifetime, with a
  two-minute window — rather than a stronger one the implementation does not
  offer.

## 9. Documentation and operations

- `docs/providers.md` — the agent enrollment section: the ticket format, the
  adopted credential, the two-minute ticket window, the exact one-time
  guarantee, the member role boundary, the operator gate, the revocation
  behaviour, and the startup reconciliation.
- `architecture/overview.md` — the two new member routes are registered in the
  managed-user route list.
- `AGENTS.md` — the Layout section already names `agents/` as a top-level
  runtime module (M001); this milestone adds no new top-level module.
- `rust/config.example.toml` — unchanged by this milestone.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| high | The live ticket producer in `eggstack/eggpass#10` does not exist. | The enrollment journey is proven only against the in-repository fixture producer, so no user can yet produce a real ticket. The EggPool-side capability is complete. | Land the `eggpass eggpool` ticket producer with the agreed `epagent1` format, then re-run the enrollment journey against a live producer. This condition is an external interface, not an M002 implementation defect. |
| high | The repository-wide dependency-audit gate is red: `cargo deny check` reports advisories FAILED for RUSTSEC-2026-0492 and RUSTSEC-2026-0495 against `russh 0.62.7`, reached through `eggress-transport-ssh 1.0.12` → `eggress-outbound 1.0.12`, which the default `ssh` feature enables. | The `dependency-audit` workflow gates on `rust/Cargo.toml`/`rust/Cargo.lock`/`deny.toml`, so it will fail for this branch. The condition is a pre-existing SSH-transport advisory, not an Agent-path defect. | Owned by Security hardening M008. Resolution requires an Eggress release permitting `russh >= 0.65`, or an accepted ADR for a vendored patched `0.62.x`, or an accepted ADR removing the default `ssh` feature. No advisory waiver is acceptable and none was added. |
| medium | The integration fixture proves server-side TLS with CA pinning, but not mutual TLS: it does not exercise `require_client_cert`. | The mTLS branch of the reverse server's client-certificate handling is configured and validated but not proven end-to-end by an automated test. Carried forward from M001's closure record. | Prove the mTLS branch when a per-agent client certificate is issued. M002 adopted the ticket secret as the transport credential rather than issuing a client certificate, so the branch stays unexercised. |
| low | The one-time ticket guarantee does not survive an EggPool restart. | A restart inside the ticket's two-minute validity window can permit reuse of a consumed ticket. | Accepted and stated explicitly in the code, the documentation, and the operator surface. Persisting an expiring consumed-ticket digest would close the window and is the named alternative. |

Scope evidence for the high dependency-audit finding: the identical
`cargo deny check` run against the pre-M001 commit `3b90434b` in a detached
worktree fails with the same two advisories, and M002 adds no dependency at
all, so the finding is pre-existing, repo-wide, and neither introduced nor
worsened by M002.

## 11. Roadmap disposition

Conditionally closed with a named outstanding condition: the live ticket
producer in `eggstack/eggpass#10` must exist before this branch lands on
`main`. That condition is an external interface dependency rather than an M002
defect, and it does not block the next milestone's implementation, so M003 is
promoted to `ready` with the promotion recorded.

M002's own capability boundary is complete: no partially implemented
enrollment path, no unrun required test, no unimplemented security or
migration requirement, and intact `--no-default-features` parity.

## 12. Registry updates

Applied in the same commit as this record:

- `plans/registry.md` — the Provider agents roadmap row records M002 as
  conditionally closed and M003 as ready; the M002 row moves from the
  dependency-ready table to the recently-closed table; the blocked-work row for
  M002 is retired; a dated audit note records the closure, the condition, and
  the promotion reasoning.
- `plans/subsystems/provider-agents-roadmap.md` — the milestone status table
  records M002 as conditionally closed and M003 as ready, and the dependency
  edges record the satisfied M002 prerequisites.
- `plans/implementation/provider-agents/002-owner-enrollment-and-revocation.md` —
  header status becomes `implemented`.
- `plans/implementation/provider-agents/003-agent-model-origin-and-health.md` —
  header status becomes `ready`.
