# Security Hardening Milestone 004 — Personal credential scope enforcement

Status: active

Repository baseline: `dcbd820e9b978d47bd43d237eded1f488d3449ee`

Source roadmap:

- `plans/subsystems/security-hardening-roadmap.md#milestone-4--personal-credential-scope-enforcement`

Long-term requirements:

- `plans/000-long-term-specification.md#2-end-state-invariants-normative` —
  item 12, identity-bound policy and accounting
- `plans/000-long-term-specification.md#3-ownership-boundaries-normative`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Applicable ADRs:

- `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` — the
  credential and principal model is unchanged.
- `plans/adrs/ADR-0004-browser-session-csrf-delivery-and-account-scoped-sign-in-throttling.md`
  — session authority is unchanged.

This plan implements a contract the schema and issuance path already persist.
It mints no new credential, grants no new authority, and removes access from
no currently issueable credential. If review judges that enforcing a dormant
field is an auth-semantics change, stop and write the required ADR before
implementation.

Primary class: invariant

## 1. Objective

Make `user_credentials.scopes` authoritative for agent tokens: a token may
use only the endpoint family its stored scopes permit, and a denial is a typed
response rather than silent access.

## 2. Why this milestone is ready

No hard dependencies. Every issued token already carries
`inference,discovery,integrations`; the principal already carries the parsed
scope list; the token-acceptance gate is one function in one middleware.
Narrowed credentials can be constructed directly in tests, so the enforcement
is provable without changing issuance.

## 3. Current implementation evidence

- `rust/src/identity.rs` — `verify_api_token_principal` parses
  `user_credentials.scopes` into `AuthenticatedPrincipal.scopes`;
  `issue_api_token` and the browser issuance path write
  `inference,discovery,integrations`.
- `rust/src/server/middleware.rs` — `personal_credential_allowed` decides
  which paths accept a personal credential. No path reads
  `principal.scopes`. The session branch inserts a principal with
  `inference,discovery`; `legacy_shared_key()` carries `inference` only.
- `rust/src/identity.rs` — `AuthenticatedPrincipal.legacy_shared_key` and the
  session principal are instance-scoped or browser-scoped authorities, not
  agent credentials.
- `rust/tests/on_prem_sessions.rs` builds personal tokens and already covers
  token failure throttling; it has no scope case.
- `docs/api-reference.md` and `architecture/deep-dive-security.md` describe
  token authentication but not a path-to-scope mapping.

## 4. Invariants that must not regress

- The token selector/digest check, constant-time comparison, negative cache,
  revocation, auth-epoch fence, and per-peer throttle are unchanged.
- A browser session's access is unchanged, including `/api/user-budget`.
- A legacy shared key's access is unchanged while `legacy_unmigrated` remains.
- Every credential issued before this milestone keeps its current access.
- Denial is a 403 with a typed JSON detail, not a 401 (a retry with the same
  token cannot help); the token is not revoked and no lockout is consumed.
- No scope value is echoed from the credential into the response.

## 5. Scope

### In scope

- A static path-to-required-scope mapping for agent tokens:
  `/v1/chat/completions`, `/v1/messages`, `/v1/responses`,
  `/v1/responses/compact`, `/v1/embeddings`, and `/api/user-budget` require
  `inference`; `/v1/models` requires `discovery`;
  `/api/integrations/v1/profile` requires `integrations`.
- Enforcement only for `PrincipalKind::AgentToken`.
- A typed denial, a security event (soft M003), tests, and documentation.

### Explicitly out of scope

- Selectable scopes at issuance, scope expiry, or per-scope budgets.
- Enforcing scopes for browser sessions or the legacy shared key.
- A scope grammar, wildcard, or inheritance model.
- Any inference-protocol or integration-profile shape change.

## 6. Required production changes

- `rust/src/server/middleware.rs`: after a personal token resolves to an
  `AgentToken` principal, resolve the required scope for the request path and
  refuse when the principal lacks it. Unknown `/v1/*` paths already fall
  through to route-level 404; they must not gain access.
- `rust/src/identity.rs`: optionally add a typed `required_scope` helper next
  to `PrincipalKind` so the mapping is testable without the middleware.
- `rust/src/web_session.rs` or the M003 sink: emit `scope_denied` when the
  sink exists.
- No schema, config, or dependency change.

## 7. Ordered work packages

### Work package A — Mapping and gate

Intent: one authoritative mapping with a fail-closed default.

Required changes: implement the mapping; enforce after token verification and
before the handler; leave non-agent principal kinds untouched; document the
mapping in code next to the enum.

Acceptance evidence: a token with only `inference` is accepted on
`/v1/chat/completions` and `/api/user-budget`, refused with 403 on
`/v1/models` and `/api/integrations/v1/profile`; a token with only
`discovery` is refused on inference; a token with all three behaves exactly
as today.

### Work package B — Evidence and regressions

Intent: prove no existing credential loses access.

Required changes: extend `rust/tests/on_prem_sessions.rs` with a narrowed
token fixture; assert the full-scope token still reaches every gated surface;
assert browser-session and legacy-key paths are unchanged; assert the 403 body
is typed and contains no scope echo; assert no throttle or revocation is
consumed.

Acceptance evidence: the new tests fail before the gate and pass after; the
existing session, token-throttle, and legacy-key tests pass unchanged.

### Work package C — Documentation

Intent: make the scope contract visible.

Required changes: `docs/api-reference.md` gains the path-to-scope table;
`architecture/deep-dive-security.md` states that agent scopes are enforced and
that sessions and legacy keys are outside the gate; note the invariant that
issuance currently grants all three.

Acceptance evidence: the documented table matches the code mapping exactly;
the release-docs validator passes.

## 8. Failure, cancellation, restart, contention semantics

The gate is a pure comparison after an existing database lookup; there is no
new I/O. A revoked or disabled credential is still refused earlier by the
existing checks. Restart and reload are irrelevant: scopes travel with each
request's principal. Concurrent requests with the same token observe the same
decision.

## 9. Compatibility and migration

No migration. Every existing agent token carries all three scopes, so no
caller loses access. A future narrower issuance is the only way to observe a
denial. No config change.

## 10. Required tests

- `rust/tests/on_prem_sessions.rs` — full-scope acceptance on every gated
  path; narrowed-token 403 matrix; session and legacy-key equivalence; typed
  body; no scope echo.
- Unit — mapping covers every `personal_credential_allowed` path and no other.
- Negative — an unknown `/v1/*` path is 404, not 403.
- Full serial workspace suite.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test on_prem_sessions -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test server_auth -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_identity -- --test-threads=1
uv run python scripts/validate_release_docs.py
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
```

## 12. Documentation updates

- `docs/api-reference.md` — path-to-scope table.
- `architecture/deep-dive-security.md` — enforcement boundary and the
  non-agent exceptions.
- `architecture/overview.md` §13 if it describes credential authority.
- `plans/registry.md` status changes.

## 13. Acceptance criteria

- An agent token lacking the required scope receives a typed 403 on the gated
  path and no backend call is attempted.
- A full-scope token has exactly its previous access.
- Sessions and legacy shared keys have exactly their previous access.
- The mapping is exhaustive over the personal-credential paths.
- No scope value is echoed, logged, or persisted in the denial.

## 14. Stop conditions

Stop and report if:

- review requires an ADR for the auth-semantics change;
- a path in `personal_credential_allowed` has no defensible required scope;
- enforcement would change a session's or legacy key's behavior;
- a test fixture cannot mint a narrowed credential without a product-facing
  issuance change.

## 15. Closure evidence required

- The mapping table and its code location.
- The narrowed-token acceptance/denial matrix.
- Session and legacy-key equivalence evidence.
- A statement that issuance, throttling, revocation, and schema are unchanged.

## 16. Handoff notes

Do not add a scope picker or change issuance in this milestone. The safe
default for an agent token on an unknown mapped path is denial, but unknown
paths must remain ordinary 404s rather than revealing which paths are gated.
Tests run serially.
