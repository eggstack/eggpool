# W003 — Static Wire-Profile Registry and Codec Contract

Status: planned; blocked on W002 closure

Source roadmap: `migration-rs/subsystems/canonical-wire-roadmap.md#w003--static-wire-profile-registry-and-codec-contract`

Primary class: capability/invariant

Hard dependency: W002 accepted closure.

## 1. Objective

Port the static wire-profile vocabulary and built-in codec registration contract that binds provider-configured profiles to the canonical IR. Establish one closed Rust codec interface and validate `_wire_profiles.toml` behavior without importing Python's runtime wire negotiation state.

## 2. Python oracle

Primary sources:

- `src/eggpool/wire/types.py`;
- `src/eggpool/wire/registry.py`;
- `src/eggpool/wire/codecs/base.py`;
- `src/eggpool/providers/_wire_profiles.toml`;
- static portions of provider configuration that reference wire profiles.

`src/eggpool/wire/resolver.py` is explicitly **not** an implementation target in W003 except as evidence of the M7 boundary.

## 3. Supported wire identities

Define typed Rust enums/structs for the stable surface/profile vocabulary frozen by W001. At minimum preserve identities for:

- OpenAI Chat Completions;
- OpenAI Responses;
- Anthropic Messages;
- Gemini generateContent;
- request/response family/surface facts that affect codec selection;
- configured request paths and other non-secret static profile metadata currently represented in `_wire_profiles.toml`.

Reject unknown profile IDs in contexts where Python rejects them. Do not silently alias an unsupported profile to a generic OpenAI shape.

## 4. Embedded/static profile data

Consume `_wire_profiles.toml` as the canonical static provider-profile data source where practical. Prefer build-time/include-time parsing/validation using the existing `toml` dependency rather than duplicating the table manually in Rust.

Requirements:

- deterministic profile ordering;
- duplicate IDs rejected;
- missing required family/surface/path/codec fields rejected;
- invalid enum values rejected;
- provider references to unknown profiles fail validation;
- profile parsing never depends on secrets/environment-specific API keys;
- semantically equivalent formatting changes to TOML do not alter profile identity.

Do not add a generated-code framework unless the small table proves impossible to maintain safely otherwise.

## 5. Codec interface

Define a small closed interface, for example `WireCodec`, supporting pure operations such as:

- decode client request JSON/value to W002 canonical request;
- encode canonical request to a selected upstream profile body/value;
- decode finite upstream response/error payload to canonical response/evidence;
- encode canonical finite response to a selected client profile;
- create/select a profile-specific stream decoder/encoder adapter for W008;
- report adaptation warnings/losses through typed metadata rather than logs alone.

The interface should operate on typed profile/context values, not provider account credentials or HTTP clients.

Avoid trait-object complexity where an enum dispatch is simpler. A closed built-in codec enum is acceptable and may be preferable on SBCs.

## 6. Codec errors and adaptation outcome

Freeze typed result structures that later plans share. Separate at least:

- malformed source request/response/event;
- unsupported wire/profile;
- unsupported semantic feature;
- conversion rejected by loss policy;
- resource/limit violation delegated from W002/W007/W008;
- provider error evidence decoded successfully from a valid provider error shape.

A provider-reported error is not necessarily a codec parse error. Preserve that distinction for M7.

Provide stable warning/loss records with semantic field/category and source/target profile, without embedding raw request/provider bodies.

## 7. Static profile lookup

Expose deterministic read-only operations such as:

- lookup by exact profile ID;
- profile -> codec family/surface;
- profile -> configured request path/static body-shaping flags;
- list profiles supported by a provider configuration in declared order if order is semantically relevant;
- validate whether a client/upstream profile pair has a codec/adaptation path.

This may supply candidate identities to M7 later, but W003 must not choose based on runtime success/failure or persist preference.

## 8. Hard M7 boundary: no dynamic resolver

Do not port these `wire.resolver` responsibilities into M6:

- account/model/provider learned wire preference;
- SQLite/runtime preference hydration/persistence;
- rejected wire-key sets from prior attempts;
- negotiation handles whose state changes after a response;
- alternate-wire selection after HTTP rejection;
- wire failure classification/retry policy.

If M7 later needs ordered static candidates, expose a pure list from W003 and let M7 own mutable attempt/negotiation state.

## 9. Provider configuration integration

Update Rust config validation only as needed to parse the currently supported static profile references without making provider HTTP behavior executable. Ensure:

- current Python-compatible configs remain accepted;
- invalid/unknown profiles fail before serving;
- no profile validation performs network I/O;
- existing M4 provider transport remains profile-agnostic except for caller-supplied method/path/body metadata.

Do not make W003 a second provider client layer.

## 10. Serialization/debug policy

Profile IDs/families/surfaces/paths are non-secret and may appear in diagnostics. Codec outcomes may expose stable semantic warning/error codes. Do not include:

- auth headers;
- API keys;
- proxy credentials;
- raw request/response bodies;
- raw session identifiers.

## 11. Required tests

1. Every W001 built-in profile fixture parses and maps to expected typed identity.
2. Registry inventory exactly matches `_wire_profiles.toml` semantic entries.
3. Duplicate/unknown/malformed profile definitions fail deterministically.
4. Provider config references to unknown profiles fail closed.
5. Profile lookup/order is deterministic independent of hash-map insertion order.
6. Every supported profile maps to exactly one codec family/dispatch target.
7. Client/upstream compatibility queries match W001 observations.
8. Codec outcome/error serialization has stable reason codes.
9. No dynamic resolver/preference/retry state is created.
10. Existing config/M4/M5 tests remain green.

## 12. Dependency/resource posture

Reuse existing `serde`, `serde_json`, and `toml`. Do not add a plugin system, dynamic registry, reflection framework, or new HTTP/TLS dependency. Keep lookup data immutable after construction and share it cheaply.

## 13. Verification

Run:

```text
cargo fmt --manifest-path rust/Cargo.toml -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
uv run pytest <targeted Python wire registry/profile/config tests> -q --tb=short --maxfail=1
git diff --check
```

## 14. Acceptance criteria

W003 closes only if:

- static profile inventory/config validation matches Python;
- all profile identities have a deterministic closed codec mapping;
- later codec plans can implement against one stable Rust interface;
- provider error evidence is distinguished from codec errors;
- dynamic negotiation/retry state remains absent;
- no new dependency is introduced without documented necessity;
- W004 may be promoted without reopening profile architecture.

## 15. Stop conditions

Do not close if an unknown profile silently falls back, profile selection depends on hash order, the codec interface requires HTTP/account secrets, static registry work ports `WireProfileResolver` retry state, or W004/W005 would need incompatible parallel codec APIs.

## 16. Closure evidence

Create `migration-rs/closure/canonical-wire/003-status.md` with profile inventory evidence, malformed-config matrix, codec contract summary, verification commands, dependency review, and registry transition promoting W004.
