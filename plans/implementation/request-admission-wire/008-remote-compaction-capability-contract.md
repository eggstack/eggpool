# Request Admission and Wire Milestone 008 — Remote compaction capability contract

Status: implemented

Repository baseline: `e6822d8f78838b273cee2351d14e5d5dd3ee1e10`

Source roadmap:

- `plans/subsystems/request-admission-wire-roadmap.md#7-milestones`

Long-term requirements:

- `plans/000-long-term-specification.md` — §2 end-state invariants, §3 ownership boundaries
- `plans/001-terminology-and-domain-model.md` — normative language for provider wire surfaces and capability advertisement
- `plans/002-long-term-roadmap.md` — sustaining work must preserve a working proxy and current compatibility

Applicable ADRs:

- `plans/adrs/ADR-0003-remote-compaction-capability-contract.md` (accepted) —
  required before implementation. The milestone implements the decision this
  ADR records. If the ADR is amended or rejected, the milestone scope changes
  accordingly and must not be implemented against the current plan text.

Primary class: capability

## 1. Objective

Give the public `POST /v1/responses/compact` endpoint a defined capability
contract: an explicit, typed failure when no eligible provider surface
advertises remote compaction, and operator-facing documentation that states
the capability requirement plainly.

## 2. Why this milestone is ready

Hard dependency: ADR-0003 must be accepted. The milestone implements a public
compatibility contract, and the decision about what that contract is is the
ADR.

No other hard dependency. The endpoint, its admission path, its wire
capability surface, and its compatibility suite already exist.

## 3. Current implementation evidence

**The endpoint is fully built.** `rust/src/coordinator/endpoints.rs` exposes
a dedicated finite execution path for the compact operation, with its own
admission and its own model-resolution check. `rust/src/request/admission.rs`
has a dedicated compact admission function that enforces the body limit, the
stateless policy, the rejection of a client-supplied compaction trigger, the
rejection of streaming, and the input-presence rule.
`rust/src/coordinator/finite.rs` attaches the compaction capabilities to the
wire runtime context so trigger validation and compact decoding observe the
same operator facts as attempt preparation.
`rust/src/wire/adapters.rs` projects the provider surface configuration into
the kernel's compaction capability type.
`rust/tests/codex_compaction_compat.rs` holds a dedicated compatibility
suite.

**The capability is operator-configured and defaults to off.**
`rust/src/config.rs` defines `supports_remote_compaction_v1` on a provider
wire surface configuration with a default of `false`, and validates that a
surface advertising the flag also supplies a `compact_path_template`.
`rust/src/provider_profile.rs` projects the flag into a provider profile.

**No provider advertises it.** The bundled provider profile asset at
`rust/crates/eggpool-provider-profile/assets/_provider_profiles.toml`
contains no compaction opt-in. `rust/config.example.toml` carries the flag
only inside a commented-out example block. The repository's provider-profile
rules require first-party evidence plus a runtime guarantee before a bundled
capability claim is asserted, and the endpoint's own compatibility suite
constructs its capable context explicitly rather than from a bundled
profile.

**The consequence is an indistinguishable failure.** With no capable surface
configured, a compact request finds no eligible candidate and fails through
the ordinary selection path. An operator cannot distinguish "this
installation has no remote compaction capability" from "this account is
unavailable" or "this model is not routable".

**The streaming path deliberately does not carry compaction capabilities.**
`rust/src/coordinator/streaming/coordinator.rs` builds its wire runtime
context without the compaction capabilities that `rust/src/coordinator/finite.rs`
attaches. The compact operation is finite-only, so that asymmetry is
correct and must be preserved.

**The operator-facing documentation is oblique.** The API reference describes
the behavior as opt-in forward compatibility rather than as a capability
contract with a stated requirement.

## 4. Invariants that must not regress

- The compact operation stays finite-only. Streaming compaction remains
  rejected at admission.
- The compact admission rules stay unchanged: the body limit, the stateless
  policy, the rejected client compaction trigger, and the input-presence rule.
- The compact path keeps its dedicated execution call and its dedicated model
  resolution check.
- The provider wire surface configuration keeps its existing keys, its
  existing default of `false`, and its existing validation that a surface
  advertising the flag also supplies a compact path template.
- The bundled provider profile asset gains no compaction opt-in under this
  milestone.
- No secret, prompt, raw body, or cache key appears in a capability failure
  body.
- The selection lock is unchanged; the capability check must not hold it
  across any await.
- No public HTTP route is removed.

## 5. Scope

### In scope

- The distinguishable capability outcome when no eligible provider surface
  advertises remote compaction.
- The operator-facing documentation of the capability requirement.
- The API reference change describing the new outcome.
- Regression tests and guards for the capability contract.

### Explicitly out of scope

- Adding a bundled provider profile that advertises remote compaction. ADR-0003
  defers that to a separate, evidence-gated change.
- Any change to the compact admission rules.
- Any change to the wire kernel's compaction capability type or the kernel
  boundary.
- Any change to the streaming path's context construction. The finite-only
  asymmetry is intentional and must be preserved.
- Any change to the remote compaction version 2 capability flag.
- Any change to the client-visible response of a successful compact request.

## 6. Required production changes

**Distinguishable capability outcome.** When a compact request cannot find
an eligible provider surface advertising remote compaction, the endpoint must
return an outcome that is distinguishable from an ordinary selection
failure. The distinction must be observable by a client, so it needs a
status code or a stable error code that the API reference can document.

The outcome must be computed before submission. A compact request must not be
dispatched to an upstream that lacks the capability, and the request must not
consume a provider attempt.

**Failure content.** The capability failure must not disclose account names,
credential material, provider secret configuration, or request body
material. It may name the operator-facing configuration keys that must be set,
because that is the actionable information and it is not secret.

**Documentation.** The API reference must state the capability requirement
plainly, name the configuration keys, and document the distinguishable
outcome. The statement that the behavior is opt-in forward compatibility must
be replaced with the actual contract.

**Guard.** Add a coverage guard asserting the bundled provider profile asset
contains no remote compaction opt-in, so a future capability claim cannot
enter it without first-party evidence and an explicit decision.

## 7. Ordered work packages

### Work package A — Capability outcome classification

Intent:

Distinguish "no remote compaction capability" from "no eligible account".

Required changes:

- Classify the compact selection failure so the capability case is
  distinguishable.
- Return the distinguishable outcome without dispatching to an upstream and
  without consuming a provider attempt.

Acceptance evidence:

- A test asserts a compact request against an installation with no capable
  provider surface returns the distinguishable outcome.
- A test asserts the response distinguishes the capability case from the
  ordinary no-eligible-account case.

### Work package B — Failure content bound

Intent:

Keep the capability failure free of sensitive material.

Required changes:

- Ensure the capability outcome body contains no account name, credential
  material, provider secret configuration, or request body material.

Acceptance evidence:

- A test asserts the capability failure body contains none of the seeded
  credential-shaped or account-identifying material.

### Work package C — Operator-facing documentation

Intent:

State the capability requirement plainly.

Required changes:

- Replace the forward-compatibility phrasing in `docs/api-reference.md` with
  the actual capability contract.
- Name the configuration keys an operator must set.
- Document the distinguishable outcome.

Acceptance evidence:

- The release documentation validation passes.
- A reviewer can determine from the API reference alone what a capability
  failure means and what to configure.

### Work package D — Bundled profile capability guard

Intent:

Keep an unverifiable capability claim out of the bundled asset.

Required changes:

- Add a guard asserting the bundled provider profile asset advertises no
  remote compaction capability.

Acceptance evidence:

- The guard fails when a compaction opt-in is added to the bundled asset.

## 8. Failure, cancellation, restart, contention semantics

- The capability outcome must be produced without a provider attempt, so no
  attempt record, no quota reservation, and no durable attribution row is
  created for a capability failure.
- A cancellation between the capability check and the outcome must not leave
  a reservation behind.
- A restart does not change the capability set; it is configuration-derived.
- The capability check must not hold the selection lock across an await.
- No change to retry behavior; the compact operation does not retry on a
  capability failure.

## 9. Compatibility and migration

- No migration. No storage change.
- No configuration key is added, removed, or reclassified.
- One public response contract changes: a compact request that finds no
  capable provider surface now returns a distinguishable capability outcome
  instead of a generic selection failure. Clients that treat any
  non-success compact response as a failure continue to work; clients that
  branch on the status code must be covered by the API reference change.
- The existing compact compatibility suite must continue to pass unchanged.

## 10. Required tests

- `rust/tests/codex_compaction_compat.rs`: the existing compatibility suite
  must continue to pass; add the capability-outcome case here if the suite
  owns the endpoint contract, otherwise in a focused target.
- `rust/tests/wire_profiles.rs` or the closest existing provider-profile
  target: the bundled-profile capability guard.
- A focused test target may be added under `rust/tests/` for the capability
  contract. Name it for the behavior, not for a milestone number.
- All Rust tests run with `--test-threads=1`.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test codex_compaction_compat -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_profiles -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
uv run python scripts/validate_release_docs.py
```

## 12. Documentation updates

- `docs/api-reference.md` — the capability contract, the configuration keys,
  and the distinguishable outcome.
- `architecture/deep-dive-transcoder.md` — record the capability contract and
  the finite-only asymmetry between the finite and streaming contexts.
- `architecture/overview.md` — only if the endpoint's status in the route
  topology needs the capability note.
- `plans/adrs/ADR-0003-remote-compaction-capability-contract.md` — only if
  the ADR is amended. An accepted ADR is not rewritten to match an
  implementation.

## 13. Acceptance criteria

- A compact request against an installation with no capable provider surface
  returns a distinguishable capability outcome that identifies the missing
  capability.
- The capability outcome creates no provider attempt, no quota reservation, and
  no durable attribution row.
- The capability outcome body contains no account-identifying, credential, or
  request body material.
- A compact request against a configured capable surface still succeeds and
  still reaches the upstream.
- The compact admission rules, the compact compatibility suite, and the
  finite-only asymmetry are unchanged.
- The bundled provider profile asset advertises no remote compaction capability.
- The API reference states the capability contract plainly, and the release
  documentation validation passes.
- The default and no-default workspace suites pass.

## 14. Stop conditions

Stop and report rather than improvise when:

- ADR-0003 has not been accepted, or its decision changes during
  implementation.
- The implementation would require a bundled provider profile capability
  claim without first-party evidence.
- The implementation would remove the public route or change the compact
  admission rules.
- The implementation would require the streaming path to carry compaction
  capabilities.
- The capability outcome cannot be made distinguishable without an
  unacceptable public contract change; in that case, stop and propose a
  separate decision.

## 15. Closure evidence required

- The implementation commits.
- A requirement-to-evidence matrix mapping each work package to the specific
  test that proves it.
- The exact verification commands run and their outcomes, labeled honestly
  between local and CI runs.
- Evidence that the pre-milestone failure was indistinguishable from an
  ordinary selection failure.
- The API reference diff showing the documented capability contract.
- The ADR acceptance record, referenced by revision.
- Any severity-tagged unresolved finding, with a recommendation.

## 16. Handoff notes

Do not start until ADR-0003 is accepted. The plan text is written against the
ADR's Option A decision; if the decision changes, return to the plan rather
than improvising an implementation.

The temptation to add a bundled provider opt-in is the main hazard here.
The provider-profile rules exist for exactly this case, and an unverifiable
capability claim in the shared contract is worse than an explicit capability
failure.

Do not "fix" the finite-versus-streaming compaction context asymmetry. The
compact operation is finite-only, and ADR-0003 records that asymmetry as
intentional.

Rust tests must run serially. Strict Clippy with `-D warnings` is an
invariant of this repository. Preserve unrelated user changes in the working
tree.
