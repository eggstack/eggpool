# Security Hardening Milestone 007 — Service-unit and HTTP response hardening

Status: active

Repository baseline: `dcbd820e9b978d47bd43d237eded1f488d3449ee`

Source roadmap:

- `plans/subsystems/security-hardening-roadmap.md#milestone-7--service-unit-and-http-response-hardening`

Long-term requirements:

- `plans/000-long-term-specification.md#2-end-state-invariants-normative` —
  items 8 (secret-free diagnostics) and 11 (single-instance local authority)
- `plans/000-long-term-specification.md#3-ownership-boundaries-normative`
- `plans/002-long-term-roadmap.md#phase-4--operations-integrations-and-deployment`

Applicable ADRs:

- `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` — the
  local operator boundary is unchanged.
- `plans/adrs/ADR-0004-browser-session-csrf-delivery-and-account-scoped-sign-in-throttling.md`
  — cookie attributes and CSRF delivery are unchanged.
- No new ADR: the sandbox and response headers do not change authentication,
  session, or reload semantics.

Primary class: invariant

## 1. Objective

Two bounded hardening outcomes: the production service unit runs with a
narrower kernel and filesystem surface, and every HTML response carries the
baseline browser protections, from one shared helper, with drift guards.

## 2. Why this milestone is ready

No dependencies. The tracked unit and the renderer are both plain text with
existing rendering tests; every HTML path already funnels through two
constructors; the response-header change is additive and compatible with the
existing inline scripts.

## 3. Current implementation evidence

- `deploy/eggpool.service` — `NoNewPrivileges`, `ProtectSystem=strict`,
  `ProtectHome`, `PrivateTmp`, kernel tunables/modules, control groups,
  SUID/SGID, namespaces, realtime, and personality are set. It has no
  `CapabilityBoundingSet`, `AmbientCapabilities`, `UMask`, `PrivateDevices`,
  `ProtectClock`, `ProtectHostname`, `ProtectProc`, `ProcSubset`,
  `ProtectKernelLogs`, or explicit privileged/mount/debug syscall denial.
- `rust/src/operations/deploy.rs::render_production_systemd` renders a second
  unit. In addition to `ExecStart` (binary path and `--verbose`) and comments,
  the renderer grants `/var/backups/eggpool` in `ReadWritePaths`; the tracked
  unit does not. Nothing compares the two, so drift is silent. The plan's
  initial claim that only `ExecStart` differed was stale; preserve both
  existing path semantics and make both differences explicit in the guard.
- `rust/tests/operations_o009.rs` covers deployment rendering but not a
  directive-level comparison with the tracked unit.
- `rust/src/server/dashboard/response.rs::html_response` sets only
  `Content-Type`.
- `rust/src/server/auth.rs::sign_in_response` and the account/member/refusal
  pages set `Cache-Control: no-store` through `no_store`, but no framing,
  sniffing, or referrer policy.
- `rust/src/server/dashboard/admin.rs::one_time_token_page` adds
  `Referrer-Policy` and `X-Content-Type-Options` to that one page only.
- `rust/tests/server_auth.rs` and `rust/tests/on_prem_admin_ui.rs` assert
  status codes and bodies, not response headers.
- The dashboard layout contains inline scripts and one inline `onclick`; a
  strict `script-src` policy is therefore deferred.

## 4. Invariants that must not regress

- The service still starts under a disposable systemd host on a supported
  target; it still binds, reads config, writes the database/log, and serves
  inference.
- `ReadWritePaths` covers every path the process writes; no new path is opened.
- The unit text stays valid for the supported systemd versions; directives that
  are unknown to an older systemd must be verified as ignored-with-warning, not
  a start failure.
- `no-store` remains on session and sign-in responses.
- JSON responses and static assets are not given HTML-only headers.
- The existing one-time token page's `no-store`, referrer, and sniffing
  behavior is preserved by the shared helper.
- No CSP directive restricts scripts or styles in this milestone.

## 5. Scope

### In scope

- Hardening directives in both the tracked and rendered production units,
  kept in sync by a comparison test with explicit allowlists for `ExecStart`
  and the existing `/var/backups/eggpool` `ReadWritePaths` difference.
- One shared response-header helper applied by the two HTML constructors.
- `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`,
  `X-Frame-Options: DENY`, and
  `Content-Security-Policy: frame-ancestors 'none'; base-uri 'none'; form-action 'self'`.
- Tests and documentation.

### Explicitly out of scope

- A script/style CSP, nonce plumbing, or removal of inline handlers.
- Changing the unit's `User`, `Group`, `WorkingDirectory`, `ExecStart`
  arguments, or `ReadWritePaths` semantics beyond aligning the two units.
- Personal (user-level) systemd unit hardening.
- Container, AppArmor, SELinux, or seccomp profile generation.
- `MemoryDenyWriteExecute` (the existing note explains why it is absent).

## 6. Required production changes

- `deploy/eggpool.service`: add `CapabilityBoundingSet=`,
  `AmbientCapabilities=`, `UMask=0077`, `PrivateDevices=yes`,
  `ProtectClock=yes`, `ProtectHostname=yes`, `ProtectProc=invisible`,
  `ProcSubset=pid`, `ProtectKernelLogs=yes`, `RemoveIPC=yes`, and
  `SystemCallFilter=@system-service` with `SystemCallErrorNumber=EPERM`. A
  systemd filter value uses one mode (allow-list or deny-list); do not mix
  `~@...` deny-list groups into the allow-list. The empty capability set and
  `NoNewPrivileges=yes` remain the privilege boundary. Keep the existing directives.
- `rust/src/operations/deploy.rs::render_production_systemd`: render the same
  set, preserving its `ExecStart` and `--verbose` differences.
- `rust/src/server/dashboard/response.rs` and `rust/src/server/auth.rs`: a
  shared `apply_html_security_headers(&mut Response)` helper, called by
  `html_response` and every HTML response in the sign-in/account path,
  including the one-time token page.
- No schema, config, or dependency change.

## 7. Ordered work packages

### Work package A — Unit hardening and drift guard

Intent: one reviewed hardening set in both units.

Required changes: add the directives to both renderers; add a test that
extracts `[Service]` directives, normalizes whitespace, preserves repeated
directive values, ignores comments, and asserts equality except for explicit
`ExecStart` and `ReadWritePaths` allowlists. Pin both allowlisted differences
to their reviewed values: rendered `--verbose` and the extra
`/var/backups/eggpool` rendered path. Keep the tracked file's comments.

Acceptance evidence: the guard fails when one directive is removed from either
unit; a disposable systemd host starts the service, serves `/v1/healthz`, and
writes a request.

### Work package B — HTML response headers

Intent: every HTML page gets the same baseline protections.

Required changes: implement the helper with the four static headers; apply it
in the dashboard HTML constructor and the sign-in/account/refusal/token
constructors; leave `no-store` in place; do not apply it to JSON or static
assets.

Acceptance evidence: transport tests assert all four headers on `/login`,
`/account` (session), `/admin/audit`, and a one-time token page; a JSON
endpoint carries none of them; a static asset carries none of them.

### Work package C — Documentation

Intent: make the sandbox and the browser baseline auditable.

Required changes: `docs/deployment.md` lists the new directives and the
disposable-host qualification requirement; `architecture/deep-dive-security.md`
documents the header set and the deferred script CSP;
`architecture/deep-dive-deployment.md` names the drift guard.

Acceptance evidence: docs match the code; the release-docs validator passes.

## 8. Failure, cancellation, restart, contention semantics

The unit change takes effect on restart; a failed start must be detected by the
disposable-host qualification. Response headers are per-response and
stateless. There is no concurrency or cancellation path.

## 9. Compatibility and migration

No migration. Existing operators restart to pick up the unit hardening. An
older systemd that ignores an unknown directive still starts with a warning;
the qualification host must be at the supported floor. The CSP subset is
compatible with inline scripts because it sets no `default-src` or
`script-src`.

## 10. Required tests

- `rust/tests/operations_o009.rs` — directive comparison guard and failure on
  a removed directive.
- `rust/tests/server_auth.rs` and `rust/tests/on_prem_admin_ui.rs` — header
  presence on every HTML path, absence on JSON/static.
- `rust/src/server/dashboard/tests.rs` if the helper is unit-tested there.
- Disposable-host systemd start (closure evidence, not a unit test).
- Full serial workspace suite.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test operations_o009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test server_auth -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_admin_ui -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
uv run python scripts/validate_release_docs.py
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
```

## 12. Documentation updates

- `docs/deployment.md` — unit directives and restart.
- `architecture/deep-dive-security.md` — response headers and deferred script
  CSP.
- `architecture/deep-dive-deployment.md` — unit ownership and drift guard.
- `plans/registry.md` status changes.

## 13. Acceptance criteria

- Both units carry the reviewed hardening set; the guard proves it.
- The service starts and serves on a disposable supported systemd host.
- Every HTML response carries the four headers; `no-store` is unchanged;
  JSON and static responses do not carry them.
- The one-time token page keeps its existing protections through the helper.
- No inline script or handler is removed or broken.

## 14. Stop conditions

Stop and report if:

- a hardening directive prevents startup on the supported systemd floor;
- the `@system-service` allow-list blocks a required operation (record the
  exact syscall and systemd version; adjust only after qualification);
- a header requires a nonce or breaks the dashboard's inline scripts;
- the drift guard needs to parse systemd include files or unit drop-ins that
  the repository does not own.

## 15. Closure evidence required

- The unit diff and the guard's failure-on-removal evidence.
- Disposable-host start output and a served request.
- Header assertions for every HTML path, JSON, and static assets.
- A statement that no authentication, session, or CSRF behavior changed.

## 16. Handoff notes

Do not attempt a script CSP in this milestone. The dashboard has inline
handlers; adding `script-src` without nonce plumbing would break the page.
Keep the tracked unit's explanatory comments and add a comment for every new
directive that is not self-evident.
