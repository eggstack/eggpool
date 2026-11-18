# ADR-0011: Configuration secret wrapper Rust API

Status: accepted

Date: 2026-11-18

Decision owners: project maintainers (explicit user approval recorded in the implementation conversation on 2026-11-18)

Related specification sections:

- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/000-long-term-specification.md#3-ownership-boundaries-normative`

Affected subsystem roadmaps:

- `plans/subsystems/security-hardening-roadmap.md`

## Context

Security Hardening M009 replaces plaintext configuration-owned credential
strings with a redacted, zeroizing `SecretString`. The affected fields are
public Rust fields on `ServerConfig`, `AccountConfig`, and
`ProviderStaticHeaderConfig`. Changing their types from `Option<String>` to
`Option<SecretString>` is therefore a source-compatibility change for Rust
consumers that construct or inspect these structs.

The change does not alter the TOML representation, configuration key names,
environment-variable resolution, CLI behavior, HTTP contracts, or persisted
data. TOML continues to represent each secret as an ordinary string. The user
explicitly approved the public Rust field-type change after the implementation
plan identified it as a compatibility decision.

## Decision drivers

- Configuration-owned secret copies should zeroize when dropped.
- Existing redacted `Debug` output and plain-string TOML behavior must remain.
- The scope must stay limited to the three named fields.
- The source-level compatibility impact must be explicit rather than hidden.
- No advisory exception or unrelated dependency change is authorized.

## Considered options

### Option A — Preserve `Option<String>` and defer M009

This preserves source compatibility but does not provide the planned zeroizing
wrapper for these fields. It remains the fallback if the scope cannot stay
bounded.

### Option B — Change the three public fields to `Option<SecretString>`

This provides the zeroizing configuration-owned value type while requiring
Rust callers that construct or inspect these fields to adapt. TOML and external
runtime behavior remain unchanged. `SecretString` is publicly reachable through
the configuration module and exposes explicit construction/conversion and
`expose_secret()` access rather than a `Display` implementation.

## Decision

Select Option B for exactly these fields:

- `ServerConfig.api_key`
- `AccountConfig.api_key`
- `ProviderStaticHeaderConfig.value`

The public Rust source-level type change is approved. Do not extend it to other
fields or change CLI, HTTP, configuration-key, serialized TOML, database, or
provider-wire contracts under this ADR. Preserve the existing secret-resolution
precedence and redacted formatting. This ADR records the user-approved
compatibility decision and does not authorize other public API changes.

## Consequences

### Positive

- The three configuration-owned secret values use the zeroizing wrapper.
- The compatibility change and its narrow boundary are reviewable and durable.
- Existing configuration files and runtime-facing behavior remain compatible.

### Negative

- Rust source consumers must adapt code that constructs or reads these public
  fields.
- `Config::clone` still creates independent secret copies; each copy is
  zeroized only when its own wrapper is dropped.

### Neutral or deferred

- Environment variables, CLI arguments, `.env` parsing buffers, request bodies,
  HTTP header copies, database rows, core dumps, and allocator behavior remain
  outside M009's zeroization guarantee.

## Compatibility and migration

No database migration or configuration-file migration is required. TOML
serialization and deserialization retain a plain string representation, and
environment fallback and precedence remain unchanged. Rust source consumers
must update field construction to use `SecretString` (or its supported
conversions) and read plaintext only through `expose_secret()`. There is no
compatibility shim for the old Rust field type.

## Security and reliability implications

The wrapper zeroizes configuration-owned string storage on drop and redacts
`Debug`. Cloning creates a distinct copy that is zeroized when that clone is
dropped. The guarantee does not claim that every process-memory copy is
zeroized, and does not cover the explicitly deferred sources above. The direct
`zeroize` dependency remains subject to the existing license and advisory gates;
this ADR adds no exception.

## Verification

- TOML round-trip tests prove secrets remain plain strings in the file format.
- Redaction tests prove neither the sentinel nor a plaintext `Display` output is
  available from the wrapper.
- Configuration tests prove inline, environment, account, and provider-header
  values retain their existing resolution behavior.
- Default and no-default compilation, strict Clippy, serial tests, dependency
  policy checks, and the locked release build provide implementation evidence.
- Any failed dependency advisory check remains a failure; this ADR does not
  change the gate.

## Supersession

None.
