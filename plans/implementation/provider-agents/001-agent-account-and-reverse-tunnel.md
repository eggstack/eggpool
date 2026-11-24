# Provider Agents Milestone 001 — Agent Account and Reverse-Tunnel Foundation

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
- `plans/adrs/ADR-0010-personal-upstream-account-custody.md` (binding/routing model, unchanged)

Primary class: infrastructure

## 1. Objective

Add an `agent` account kind whose endpoint is a server-local loopback port
served by an embedded Eggress reverse server, and prove a coordinator request
can be routed to it end-to-end. No enrollment UI and no removal of OAuth in this
milestone.

## 2. Why this milestone is ready

Ready.

- Hard: satisfied. ADR-0016 is `accepted` (2026-11-24). This milestone's account
  and auth-semantics change and its durable dependency are covered by that
  decision, so no further approval gate stands between the plan and execution.
- Hard: the Eggress `1.0.12` reverse server/client must embed cleanly. The crates
  are published at the pinned version (`eggress-runtime`, `eggress-protocol-reverse`,
  `eggress-embed`), and work package A qualifies the embedding surface and feature
  set inside this milestone. That qualification is a work-package gate, not a
  pre-handoff blocker; a negative result stops the milestone rather than blocking
  its handoff.
- Interface: none. The tunnel fixture is in-repository; Eggpass is not required
  for this milestone.

## 3. Current implementation evidence

- `rust/src/config.rs`: `AccountAuthMode` is `StaticKey | OAuth`; `AccountConfig`
  carries `api_key`, `api_key_env`, `enabled`, weights, offsets, and proxy
  fields, but no per-account `base_url`. `ProviderConfig.base_url` is
  provider-scoped.
- `rust/src/providers/transport.rs`: `ProviderClientPool` builds one client per
  `(provider, account)` from the provider configuration and exposes
  `oauth_eggfetch_client()`; request dispatch uses `send` and the provider
  `base_url`.
- `rust/src/coordinator/`: finite and streaming resolve an account credential
  before publication and treat an OAuth 401 as a credential-refresh signal
  (`coordinator/mod.rs`, `finite.rs`, `streaming/coordinator.rs`).
- `rust/src/oauth.rs`: the owner-scoped credential manager used by
  `resolve_account_api_key`. It must remain untouched for OAuth accounts.
- `rust/src/routing/eligibility.rs`: `personal_bindings.excludes` and
  `is_active_owner` gate account visibility; this model is reused.
- `rust/src/config_reload_policy.rs`: the only reload-vs-restart authority.
  New keys must be classified there.
- `rust/tests/provider_transport.rs`, `rust/tests/coordinator_boundaries.rs`, and
  `rust/tests/routing_domain.rs` show the fixture style for transport, owner,
  and accounting assertions.

## 4. Invariants that must not regress

- No provider access or refresh token is stored or read by EggPool for an agent
  account.
- An agent account is selectable only for its bound owner; no cross-owner
  selection or fallback.
- The reverse-server external listener is bound to `127.0.0.1` only, and the
  account `base_url` must be loopback.
- The existing `ProviderClientPool` immutability, provider-transport policy
  (Eggfetch `0.4.0`, Eggress `1.0.12`), and single generation model are
  unchanged.
- OAuth accounts behave exactly as today, including the ADR-0015 refresh path.
- Strict Clippy, serial tests, and no-default parity remain repository
  invariants.

## 5. Scope

### In scope

- `AccountAuthMode::Agent` (additive; `OAuth` remains).
- A per-account `base_url` and an agent transport configuration block.
- Per-account endpoint support in the provider client pool.
- `AgentTunnelManager`: one embedded Eggress reverse server per agent account,
  process-owned, generation-configured, atomically replaced.
- Agent-specific 401 and health behavior: no refresh, no substitution.
- Config validation and `classify_transition` coverage.
- An in-repository tunnel fixture and coordinator tests.

### Explicitly out of scope

- Ticket decoding, enrollment routes, credential issuance, revocation UI.
- Model origin labels and catalog discovery.
- Removing `[oauth]`, `AccountAuthMode::OAuth`, or the OAuth code.
- Eggpass changes; the fixture stands in for the upstream client.
- A second supervisor, config-reload path, or metrics stack.

## 6. Required production changes

**Configuration (`rust/src/config.rs`).** Add `Agent` to `AccountAuthMode`. Add an
optional per-account `base_url` and an `agent` block carrying control bind,
external bind (or port), TLS paths, `require_client_cert`, `max_streams`, and a
heartbeat. Validate: `base_url` is required for `Agent`, is `http` or `https`,
has no userinfo/query/fragment, and resolves to a loopback host. Reject an agent
block on a non-agent account and a non-loopback external bind. Keep secrets in
the existing `api_key_env`/secret path; never store a transport credential in
the TOML.

**Reload policy (`rust/src/config_reload_policy.rs`).** Classify listener binds,
TLS material, and credential paths as restart-required; classify account
enable/disable and model eligibility as live where the existing policy allows.
Do not add an independent key list.

**Client pool (`rust/src/providers/transport.rs`).** Resolve the request base URL
per account: provider `base_url` for static/OAuth, the account `base_url` for
agents. Keep the pool immutable and the client per account. The agent's local
API key uses the existing account key fields and is sent as the bearer exactly
like a static key.

**Tunnel manager (new, likely `rust/src/agents/`).** Build one reverse server per
enabled agent account from the candidate generation; start listeners before
publication; publish atomically with the generation; retire the previous manager
without dropping in-flight requests; bound shutdown. It must not own a second
reload loop.

**Coordinator (`rust/src/coordinator/`).** `resolve_account_api_key` returns the
configured key for `Agent` (no OAuth manager call). A 401 on an agent account is
terminal for that attempt: no refresh, no same-account replay, no substitution
by another account. Keep the static-key and OAuth policies unchanged.

**Health/diagnostics.** Expose whether each agent's control connection is up in
generation diagnostics only; no active probing. M003 projects it to status and
the dashboard.

## 7. Ordered work packages

### Work package A — Dependency and embedding qualification

Intent: confirm the reverse server/client can be embedded without adopting a
second supervisor.

Required changes: add the narrowest exact `=1.0.12` dependency that provides the
reverse server and client; record the resolved feature graph and duplicates;
confirm TLS client-certificate support; decide `eggress-runtime` versus
`eggress-embed`.

Acceptance evidence: `cargo tree -e features` and `--duplicates` reviewed;
`cargo deny check` and the locked release build pass; a minimal in-repo binary
test starts both ends and relays one HTTP request.

### Work package B — Account kind and configuration

Intent: make an agent account expressible and safe.

Required changes: enum variant, fields, validation, defaults, load/save and
mutation types, `classify_transition` coverage.

Acceptance evidence: config parse/reject tests for loopback, TLS paths,
duplicate ports, agent block on a non-agent account; reload classification
tests.

### Work package C — Per-account endpoint in the client pool

Intent: send requests to the account's loopback endpoint.

Required changes: thread the per-account base URL and key through the pool;
preserve provider scoping for non-agent accounts; keep proxy/transport policy
identical.

Acceptance evidence: `provider_transport` tests for agent and static accounts
against local fixtures; no change to OAuth client reuse.

### Work package D — Tunnel manager

Intent: own reverse-server lifecycle.

Required changes: build/start/publish/retire; bounded shutdown; listener errors
mapped to a typed account-unavailable state; no listener on a non-loopback
external bind.

Acceptance evidence: restart/replace test with an in-flight request; a bind
conflict fails closed without taking down the generation; shutdown leaves no
listener.

### Work package E — Routing, 401, and fixture

Intent: prove end-to-end behavior.

Required changes: agent resolution in finite and streaming paths; terminal 401
policy; fixture tunnel plus fake agent HTTP server.

Acceptance evidence: `rust/tests/agent_upstreams.rs` proves owner-only
selection, successful finite and streaming requests through the tunnel, no OAuth
refresh on 401, and no cross-owner fallback.

## 8. Failure, cancellation, restart, contention semantics

- A failed listener bind fails the candidate generation; the previous generation
  keeps serving.
- A dropped control connection refuses new streams on the external port; the
  in-flight request fails with a transport error and the account is marked
  unavailable. No automatic account substitution.
- Manager replacement waits for in-flight streams up to a bounded grace period,
  then closes them.
- Concurrent reloads are serialized by the existing publication authority; the
  tunnel manager never publishes itself.
- Enrollment is not present yet; a missing or malformed agent credential fails
  at request preparation, before provider publication.

## 9. Compatibility and migration

`Agent` is additive. Existing configurations load unchanged. `OAuth` behavior,
the credential store, and the ADR-0015 path are untouched. No storage migration.
Backup includes the new configuration and any agent credential files only when
M002 adds them.

## 10. Required tests

- `rust/src/config.rs` unit tests for parsing, validation, and rejection.
- `rust/src/config_reload_policy.rs` transition tests.
- `rust/tests/provider_transport.rs` for per-account endpoints.
- `rust/tests/agent_upstreams.rs` for the tunnel fixture, owner-only routing,
  finite/streaming success, terminal 401, and no fallback.
- `rust/tests/coordinator_boundaries.rs` for publication/preparation ordering.
- `rust/tests/routing_domain.rs` for eligibility with an agent account.
- Default and no-default serial workspace suites.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test agent_upstreams -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test routing_domain -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo deny --manifest-path rust/Cargo.toml check
cargo build --manifest-path rust/Cargo.toml --locked --release
```

## 12. Documentation updates

- `architecture/deep-dive-providers.md`: agent account transport and ownership.
- `architecture/overview.md` and `AGENTS.md`: the new `rust/src/agents/` owner.
- `rust/config.example.toml`: a commented agent account example with loopback
  placeholders and no secrets.
- `docs/providers.md`: when agent accounts exist and how they differ from OAuth.

## 13. Acceptance criteria

- `auth_mode = "agent"` parses, validates, and is rejected when the endpoint is
  not loopback or the reverse configuration is unsafe.
- A request routes to the bound owner's agent through the fixture tunnel and its
  response returns unchanged.
- An agent 401 produces no refresh and no substitution.
- OAuth accounts and their refresh path behave exactly as before.
- Full matrix passes, including no-default Clippy and the locked release build.

## 14. Stop conditions

Stop and report if: ADR-0016 is not accepted; the reverse server cannot be
embedded without a second supervisor or config-reload authority; the dependency
forces features outside the reviewed profile; a loopback external bind cannot be
guaranteed; or the change would alter OAuth behavior.

## 15. Closure evidence required

- Dependency graph, feature review, `cargo deny`, and locked build results.
- Config validation and reload-classification evidence.
- Tunnel fixture output for finite and streaming requests, owner exclusion, and
  terminal 401.
- Default and no-default serial suite results; strict Clippy.
- Confirmation that no OAuth code path changed.

## 16. Handoff notes

Eggress is exact-pinned; do not widen it. Keep the tunnel manager process-owned
and generation-configured. Do not add a second supervisor, metrics stack, active
probing, or dynamic port allocation. Rust tests run serially. The worktree may
carry uncommitted OAuth review fixes; do not fold them into this milestone.
