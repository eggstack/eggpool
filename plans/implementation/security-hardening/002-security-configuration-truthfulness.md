# Security Hardening Milestone 002 — Security configuration truthfulness

Status: active

Repository baseline: `dcbd820e9b978d47bd43d237eded1f488d3449ee`

Source roadmap:

- `plans/subsystems/security-hardening-roadmap.md#milestone-2--security-configuration-truthfulness`

Long-term requirements:

- `plans/000-long-term-specification.md#2-end-state-invariants-normative` —
  item 8, secret-free diagnostics
- `plans/000-long-term-specification.md#3-ownership-boundaries-normative`
- `plans/002-long-term-roadmap.md#phase-4--operations-integrations-and-deployment`

Applicable ADRs:

- None required. The milestone does not change reload/restart classification,
  authentication semantics, or the durable dependency set. It keeps every
  field parseable for compatibility.
- If review decides to implement a Host allowlist or a CORS layer instead of
  deprecating the keys, stop: that is a new capability with its own plan, and
  a Host policy needs an architecture decision.

Primary class: invariant

## 1. Objective

Make the configuration surface tell the truth: every `[security]` and
`[server]` key either has a runtime consumer or reports itself as reserved at
startup, and a secret-bearing configuration file that is readable by other
local users produces a warning. No control is silently absent.

## 2. Why this milestone is ready

No dependencies. The configuration contract is owned by `rust/src/config.rs`,
the startup warning pattern already exists in `rust/src/server/mod.rs`, and
the adjacent environment path resolver already exists. The work is additive
and does not change reload classification.

## 3. Current implementation evidence

- `rust/src/config.rs` — `SecurityConfig` parses `allowed_hosts`,
  `cors_origins`, `redact_headers`, `persist_redacted_error_detail`;
  `ServerConfig` parses `access_log`. Only `trusted_proxies` and
  `allow_insecure_web_auth` are read outside configuration.
- `rust/src/config_reload_policy.rs` — the inert keys are classified as
  restart-required, so an operator pays a restart for no effect.
- `rust/src/server/mod.rs` — `warn_if_unauthenticated_loopback` and
  `warn_if_insecure_web_auth` show the established warning pattern; the
  warning helpers are called on every serve path.
- `rust/config.example.toml` — advertises `allowed_hosts`, `cors_origins`,
  `redact_headers`, `persist_redacted_error_detail`, and `access_log` as if
  they were active controls.
- `architecture/deep-dive-security.md` — states that log redaction flows
  through `SecurityConfig::redact_headers`; redaction is actually hardcoded in
  `rust/src/coordinator/finalization.rs` and `rust/src/operations/status.rs`.
- `docs/deployment.md` — presents `access_log = true` as a diagnostics
  profile; `docs/raspberry-pi.md` recommends `access_log = false`. Neither has
  an effect.
- `rust/src/operations/config_mutation.rs` — new files are written `0o600`,
  but `existing_mode` preserves an operator-created mode, and startup does not
  warn about a readable file containing inline keys.

## 4. Invariants that must not regress

- Existing configurations that contain the reserved keys still parse and
  start; `deny_unknown_fields` behavior is unchanged.
- `trusted_proxies` and `allow_insecure_web_auth` keep their current effect
  and warnings.
- `config_reload_policy` classification is unchanged; no reload/restart
  boundary moves.
- Warning text contains no key value, credential, path contents, or provider
  name that is itself secret.
- Inline key resolution, environment fallback, and the `.env` path precedence
  are unchanged.

## 5. Scope

### In scope

- Report reserved-but-parsed security keys at startup.
- Correct the example configuration, the security deep dive, and the
  deployment/Pi guidance.
- Warn when the resolved config file or adjacent `.env` is group/other
  readable and the parsed configuration carries an inline secret.
- Add a vulnerability-disclosure policy file.

### Explicitly out of scope

- Implementing `allowed_hosts`, CORS, configurable log redaction, or an access
  log.
- Removing fields from the serde struct (that would break existing configs).
- Changing file modes automatically; the operator keeps ownership.
- Any authentication, session, or throttling behavior.

## 6. Required production changes

- `rust/src/config.rs`: document the reserved fields and add a typed
  `reserved_security_settings(&self) -> Vec<&'static str>` helper that returns
  only non-default values, plus a secret-presence predicate used by the
  permission warning.
- `rust/src/server/mod.rs`: render the reserved-setting warning and the
  readable-secret-file warning through the existing startup-warning path. Pass
  the resolved config path where the existing helpers are called.
- `rust/src/config.rs` or `rust/src/operations/paths.rs`: no new path logic;
  reuse `resolve_env_path` and the already-resolved configuration path.
- Root `SECURITY.md`: disclosure process. The maintainer must enable GitHub
  private vulnerability reporting as the operational step; until then the file
  says not to publish exploit details in an issue.

## 7. Ordered work packages

### Work package A — Reserved-setting inventory

Intent: one authoritative list of keys that parse but do nothing.

Required changes: implement `reserved_security_settings` with the five names
and their non-default conditions; keep the field declarations with a
"reserved" doc comment; do not touch the reload policy.

Acceptance evidence: a unit test with a default config returns no names; a
config with each non-default value returns exactly that name; a config copied
from the current example (`redact_headers` at its default, `access_log
= false`) returns none.

### Work package B — Startup warnings

Intent: an operator sees the mismatch at the moment it matters.

Required changes: add `warn_if_reserved_security_settings` beside the
existing warnings on every serve path. One warning per key, naming only the
key and stating that it has no runtime effect and where the control belongs
(operator proxy, external log pipeline, or removal). No values.

Acceptance evidence: a unit test captures the rendered warning strings; a
manual `serve` against a temp config with one reserved key shows exactly one
warning and the server still starts.

### Work package C — Secret-file permission warning

Intent: an inline-key config that is group/other readable is called out
without printing anything secret.

Required changes: inspect the resolved config path and the adjacent env path
with `symlink_metadata`; if the mode has any `0o077` bits and the parsed
configuration carries an inline server key, account key, or provider static
header value (or the env file exists at all), emit one warning per file with
the path and a `chmod 600` instruction. Do not open or print the file
contents.

Acceptance evidence: temp-dir test — `0o600` is silent; `0o644` with an inline
key warns; `0o644` without inline secrets is silent for the config file but
still warns for a readable env file; the warning text contains neither the key
nor a line of the file.

### Work package D — Documentation and disclosure

Intent: stop advertising controls that do not exist.

Required changes: remove the five keys from `rust/config.example.toml` (the
struct still accepts them); correct `architecture/deep-dive-security.md` to
describe the hardcoded redaction rules and the reserved keys; remove the
`access_log` diagnostic profiles from `docs/deployment.md` and
`docs/raspberry-pi.md`; add `SECURITY.md`.

Acceptance evidence: `git grep` finds no prose claiming the keys work; the
example still parses; `uv run python scripts/validate_release_docs.py` passes.

## 8. Failure, cancellation, restart, contention semantics

Warnings are emitted once at startup and are idempotent. A warning must never
prevent startup; only `validate_server_access` and the existing key-shape
validation may reject. Reload paths re-classify the same keys exactly as
before, so a restart-required result is unchanged.

## 9. Compatibility and migration

No migration. Existing configs keep parsing. Removing the keys from the
example changes no runtime behavior. The warning is additive. `SECURITY.md`
is a new repository file, not a runtime contract.

## 10. Required tests

- Unit: `reserved_security_settings` default/non-default matrix; renderer
  contains no value.
- Unit: permission predicate for `0o600`, `0o640`, `0o644`, missing file, and
  a symlinked path (metadata only).
- Integration: startup still succeeds with every reserved key set to a
  non-default value.
- Tooling: release-docs validator for the touched release docs.
- Full serial workspace suite; no-default check/clippy.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --lib config -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1
uv run python scripts/validate_release_docs.py
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
```

## 12. Documentation updates

- `rust/config.example.toml`, `architecture/deep-dive-security.md`,
  `docs/deployment.md`, `docs/raspberry-pi.md`.
- `SECURITY.md` (new).
- `plans/registry.md` status changes.

## 13. Acceptance criteria

- Every reserved key produces one warning when non-default and none by
  default.
- Startup succeeds with all reserved keys set.
- A `0o644` config containing an inline key warns; `0o600` does not.
- No warning or log line contains a key value or file content.
- The example and the three documents no longer claim the controls work.
- Reload classification is byte-for-byte unchanged.

## 14. Stop conditions

Stop and report if:

- the change requires altering `classify_transition` or a reload test
  expectation;
- a struct field must be removed to satisfy the plan (compatibility break);
- the warning path needs access to raw file contents;
- the project decides to implement Host validation or CORS in this milestone.

## 15. Closure evidence required

- The reserved-key inventory table (key, condition, warning text).
- The permission-warning test matrix and the startup-succeeds evidence.
- Documentation diffs and the release-docs validator output.
- A statement that `config_reload_policy.rs` and `deny.toml` are untouched.

## 16. Handoff notes

The five keys are a compatibility surface: do not remove them from the serde
struct. Keep warnings secret-free. `access_log` is `[server]`, not
`[security]`; the helper may cover both but the warning must name the exact
table.
