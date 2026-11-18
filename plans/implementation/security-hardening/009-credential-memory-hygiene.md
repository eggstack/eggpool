# Security Hardening Milestone 009 — Credential memory hygiene

Status: active

Repository baseline: `dcbd820e9b978d47bd43d237eded1f488d3449ee`

Source roadmap:

- `plans/subsystems/security-hardening-roadmap.md#milestone-9--credential-memory-hygiene`

Long-term requirements:

- `plans/000-long-term-specification.md#2-end-state-invariants-normative` —
  item 8, secret-free diagnostics
- `plans/000-long-term-specification.md#3-ownership-boundaries-normative`
- `plans/002-long-term-roadmap.md#phase-4--operations-integrations-and-deployment`

Applicable ADRs:

- `plans/adrs/ADR-0011-configuration-secret-wrapper-rust-api.md` — accepted
  user authorization for the source-level type change on the three public
  configuration fields. TOML and runtime-facing contracts remain unchanged.
- `zeroize` is already resolved in the dependency graph through the pinned
  crypto stack; declaring it directly does not change the resolved version.
- If the implementation proposes a new secret-management crate beyond
  `zeroize`, stop: that is a durable dependency decision and needs review.

Primary class: polish

## 1. Objective

Shorten the lifetime of plaintext configuration-owned credentials in process
memory by wrapping the three secret-bearing configuration fields in a redacted,
zeroizing type, without changing resolution, logging, TOML, CLI, HTTP, or runtime
behavior. The source-level Rust field-type change is approved by ADR-0011.

## 2. Why this milestone is ready

No hard dependencies. `zeroize 1.9.0` is already in `rust/Cargo.lock` through
`rsa`; the three fields are the complete set of configuration-owned provider
secrets; each already has a custom redacted `Debug` implementation, so the
observable contract is easy to preserve.

## 3. Current implementation evidence

- `rust/src/config.rs` — `ServerConfig.api_key: Option<String>`,
  `AccountConfig.api_key: Option<String>`, and
  `ProviderStaticHeaderConfig.value: Option<String>` hold provider or server
  credentials as plain strings.
- The three structs have hand-written `Debug` implementations that print
  `[REDACTED]`; `Config` derives `Clone`, so a generation clone duplicates each
  secret.
- `rust/Cargo.lock` — `zeroize 1.9.0` is present transitively; it is not a
  direct workspace dependency.
- `rust/src/operations/config_mutation.rs` and
  `rust/src/operations/integrations.rs` treat the resolved values as borrowed
  strings; no code serializes the parsed `Config` back to TOML.
- `rust/src/personal_upstream.rs` accepts a request-body secret and passes it
  to the environment-file writer; that transient path is separate from the
  configuration-owned fields.
- No core-dump policy, `mlock`, or allocator hardening exists in the
  repository.

## 4. Invariants that must not regress

- TOML parsing, environment fallback (`api_key_env`), `[providers.*.headers]`
  `value_env`, and precedence are unchanged.
- Every `Debug` output stays redacted; no `Display` implementation exposes the
  value.
- Config cloning still compiles and resolves the same values.
- No credential is serialized, logged, persisted, or exposed in an error
  message by the new type.
- `cargo deny` licenses/sources and the locked release build stay green.
- `SecretString` is public only because the three converted configuration
  fields are public Rust API. No public CLI or HTTP shape changes.

## 5. Scope

### In scope

- One `SecretString` type with `ZeroizeOnDrop`, a redacted `Debug`, a
  `Serialize`/`Deserialize` string representation, and an explicit
  `expose_secret()` accessor.
- Convert `ServerConfig.api_key`, `AccountConfig.api_key`, and
  `ProviderStaticHeaderConfig.value`.
- Add `zeroize` as a direct dependency.
- Update resolution sites mechanically and add tests.

### Explicitly out of scope

- Operating-system environment variables, CLI arguments, `.env` parsing
  buffers, request bodies, HTTP `HeaderValue` copies, database rows, or
  personal-upstream form handling.
- `mlock`, core-dump suppression, allocator hardening, or a process-wide
  secret registry.
- Changing when a secret is resolved or how it is used.
- A new secret-management dependency.

## 6. Required production changes

- `rust/Cargo.toml` — add `zeroize = "1"`.
- `rust/src/secret.rs` (new module) or the configuration module — define
  `SecretString` over `Zeroizing<String>`; implement `Debug`, `Serialize`,
  `Deserialize`, and `Clone`; provide `expose_secret(&self) -> &str`.
- `rust/src/config.rs` — change the three fields and the resolution helpers;
  keep the existing redacted `Debug` output shape (`Option` of a redacted
  marker).
- Call sites in `rust/src/providers/`, `rust/src/runtime.rs`,
  `rust/src/operations/`, and `rust/src/server/` — borrow through
  `expose_secret()`; do not add `to_owned()` unless ownership is required.
- No schema, migration, config-key, CLI, HTTP, or provider-wire change. The
  public Rust source field types change only as authorized by ADR-0011.

## 7. Ordered work packages

### Work package A — Type and dependency

Intent: one small, auditable primitive.

Required changes: add the dependency, implement the type, and add unit tests
for round-trip and redaction. Keep the module private to the crate unless a
sibling crate needs it (none does).

Acceptance evidence: `cargo tree -i zeroize` shows one version; a TOML
round-trip preserves the value; `format!("{:?}", secret)` contains
`[REDACTED]` and not the value.

### Work package B — Field conversion

Intent: convert the three configuration-owned secrets without behavior drift.

Required changes: change the fields; update resolution helpers to return
borrowed `&str`; fix the compiler-identified call sites; keep the existing
`api_key`/`api_key_env` precedence.

Acceptance evidence: config tests pass unchanged; a resolved key equals the
previous value for inline, env, and provider-header cases; the existing
`Debug` redaction tests pass.

### Work package C — Verification and documentation

Intent: prove no leak was introduced.

Required changes: add a negative test that a `Config` `Debug` or an error
string does not contain the sentinel secret; run the serial suite and the
locked release build; document the guarantee and its limits in the security
deep dive.

Acceptance evidence: no test output, panic message, or rendered config
contains the sentinel; `cargo deny` passes; the docs state that OS env,
argv, header copies, and request bodies are outside the guarantee.

## 8. Failure, cancellation, restart, contention semantics

`SecretString` is a value type; there is no runtime failure path. A cloned
`Config` holds an independent copy that zeroizes when that generation drops.
Shutdown drops the active configuration; a crash or core dump is outside the
guarantee. No concurrency or cancellation behavior changes.

## 9. Compatibility and migration

No database migration, configuration-file migration, or config-key change is
required. Existing TOML and environment behavior is identical, and the
serialized form remains a plain string. The public Rust field types change as
approved by ADR-0011, so Rust source consumers must adapt field construction and
read plaintext through `expose_secret()`. The release binary gains one direct
dependency already present in the graph.

## 10. Required tests

- Unit — TOML deserialize/serialize, redaction, clone, `expose_secret`.
- Config — inline key, `api_key_env`, provider header `value`, and
  `value_env` precedence unchanged.
- Negative — a sentinel secret never appears in `Debug`, an error string, or
  a config dump.
- `cargo deny check licenses sources` and the locked release build.
- Full serial workspace suite.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --lib config -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1
cargo deny --manifest-path rust/Cargo.toml check licenses sources
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
```

## 12. Documentation updates

- `architecture/deep-dive-security.md` — configuration-owned secret lifetime
  and the explicit non-guarantees.
- `architecture/overview.md` §2 — the configuration contract now has a secret
  wrapper.
- `plans/registry.md` status changes.

## 13. Acceptance criteria

- The three fields use the zeroizing wrapper; all resolution paths behave
  identically.
- `Debug` remains redacted and no `Display` exists.
- A sentinel secret is absent from all test-observable output.
- `zeroize` is a direct dependency with an allowed license; `cargo deny`
  passes.
- The locked release build and the serial suite pass.

## 14. Stop conditions

Stop and report if:

- the wrapper requires changing any public type beyond the three fields
  explicitly authorized by ADR-0011, a wire shape, or a database row;
- a call site needs the secret as an owned `String` in a long-lived structure
  outside the configuration;
- a second secret-management crate is proposed;
- the conversion expands into environment, argv, header, or body handling.

## 15. Closure evidence required

- The new type and its unit tests.
- The three field conversions and the resolution-path tests.
- The sentinel negative test.
- `cargo deny` and locked release build output.
- A statement of what is still not zeroized.

## 16. Handoff notes

This is deliberately partial. Do not claim that process memory is free of the
secret; claim only that configuration-owned copies zeroize on drop. Keep the
existing `Debug` strings to avoid breaking tests that assert the redaction
marker.
