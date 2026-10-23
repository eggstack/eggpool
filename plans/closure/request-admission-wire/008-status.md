# Request Admission and Wire Milestone 008 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/request-admission-wire/008-remote-compaction-capability-contract.md`

Source subsystem roadmap:

- `plans/subsystems/request-admission-wire-roadmap.md#milestone-008--remote-compaction-capability-contract`

Governing decision record:

- `plans/adrs/ADR-0003-remote-compaction-capability-contract.md` (accepted)

Repository baseline reviewed: `e6822d8f78838b273cee2351d14e5d5dd3ee1e10`

Implementation commits or pull requests:

- `8a0552e4` — Implement the remote compaction capability contract behind
  ADR-0003. This commit introduces ADR-0003 with `Status: accepted`, so it is
  the ADR's acceptance revision.
- The closure commit that carries this record is the branch revision directly
  after `8a0552e4`. It closes M008 in `plans/registry.md` and in the subsystem
  roadmap. A record cannot name the revision that contains it, so the
  implementation revision is the one named here and the closure revision is
  read from the branch history.

## 1. Executive finding

`POST /v1/responses/compact` was fully implemented, tested, and documented, but
no provider surface advertised the remote-compaction capability, so a real
request could not reach a capable upstream and failed indistinguishably from an
ordinary selection failure. ADR-0003 made the capability contract explicit, and
this milestone implemented it.

The endpoint now computes the capability answer from the operator facts that
already own it — the provider's `wire_surfaces` entry for `openai_responses`
carrying `supports_remote_compaction_v1` together with a `compact_path_template`
— and reports a typed capability outcome when no eligible provider surface
advertises the capability. The outcome is `501 Not Implemented` with the stable
error code `remote_compaction_unsupported`. The ordinary no-eligible-account
failure keeps its `503 Service Unavailable` status and generic envelope, so an
operator can tell a missing capability from a missing account.

The capability answer is produced before any provider attempt exists. The
installation-level check runs before selection, so it creates no claim, no
attempt, no quota reservation, and no durable attribution row. A route-level
capability miss — an eligible account whose provider lacks the capability — is
reported with the same typed outcome, so a routable-but-incapable provider is
not reported as a missing account.

The capability failure body names only the two operator-facing configuration
keys. It carries no account name, credential material, provider secret
configuration, or request body material.

The bundled provider-profile asset advertises no remote-compaction capability
and now says so under test, so a capability claim cannot enter the shared
contract without first-party evidence and its own approved change.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Status |
|---|---|---|
| The capability check happens during selection, before submission | `rust/src/coordinator/finite.rs::execute_input` evaluates the installation-level capability before the selection loop and before any claim or attempt | Met |
| The request must not consume a provider attempt | `a_compact_request_without_a_capable_surface_fails_with_the_capability_outcome` asserts the upstream request count is zero and that `request_attempts`, `requests`, and `reservations` all stay empty | Met |
| The request must not hold the selection lock across any await | The installation-level check reads the generation's provider table and profile table synchronously, before `select_and_claim`; no `.await` is introduced inside `RoutingRouter::selection_lock` | Met |
| The outcome must be distinguishable from the ordinary selection failure | `EndpointError::RemoteCompactionUnsupported` maps to `501`; `EndpointError::NoEligibleRoute` keeps `503`; `the_capability_outcome_is_distinguishable_from_the_selection_failure` asserts both the statuses and the stable code | Met |
| The distinction must be observable by a client | `endpoint_error_body_for` renders the stable code `remote_compaction_unsupported`; `docs/api-reference.md` documents the status, the code, and the failure semantics | Met |
| The capability failure must not disclose account names, credential material, provider secret configuration, or request body material | `the_capability_failure_body_names_only_the_configuration_keys` asserts the body carries the two configuration keys and none of the seeded credential, account, provider, or model markers | Met |
| The streaming path must not carry compaction capabilities | `the_streaming_coordinator_carries_no_compaction_capabilities` asserts the streaming coordinator never attaches compaction capabilities while the finite response context does | Met |
| The bundled profile must advertise no remote compaction capability | `embedded_data_advertises_no_remote_compaction_capability` asserts the shared asset declares neither compaction key and that no bundled surface advertises the capability | Met |
| A capable installation still serves the operation | `a_compact_request_against_a_capable_surface_reaches_the_upstream` asserts a `200` and that the upstream received the configured compact path template | Met |
| The compact operation remains finite-only | The compact admission still rejects `stream: true`; no streaming context change was made | Met |
| A missing account stays an ordinary selection failure | `the_ordinary_selection_failure_still_reports_no_eligible_account` asserts `503` and the generic envelope | Met |

## 3. Production implementation evidence

- `rust/src/coordinator/finite.rs`
  - `remote_compaction_capable(providers, provider_profiles)` — the
    installation-level predicate. It mirrors the per-account selection filter
    exactly: an OpenAI Responses wire profile whose surface advertises
    `supports_remote_compaction_v1` together with a `compact_path_template`.
    A generation with no provider configuration is neither capable nor
    incapable, because there is no surface on which the capability could be
    advertised.
  - `FiniteCoordinatorError::RemoteCompactionUnsupported` — the typed
    coordinator-level outcome.
  - `execute_input` evaluates the installation-level capability before the
    selection loop. The check is skipped for a generation with no provider
    configuration and for every non-compact operation.
  - `execute_input` records a route-level capability miss when an otherwise
    eligible account belongs to a provider without the capability. The claim
    is rolled back, so the account is excluded without upstream I/O and
    without an attempt, a reservation, or an attribution row. When the loop
    then converges with no dispatch at all, the typed capability outcome is
    reported instead of the ordinary no-eligible-route outcome.
- `rust/src/coordinator/endpoints.rs`
  - `EndpointError::RemoteCompactionUnsupported` with the `Display` text that
    names only the two configuration keys.
  - `EndpointError::status` maps it to `501 Not Implemented`. The ordinary
    selection failure keeps `503`.
  - `endpoint_error_body_for` renders the stable code
    `remote_compaction_unsupported` and the operator-facing message.
  - `map_finite_error` maps the coordinator-level outcome onto the endpoint
    error.
- `rust/src/coordinator/mod.rs` — exports `remote_compaction_capable`.
- `rust/crates/eggpool-provider-profile/tests/contract.rs` — the bundled
  capability guard.

No configuration key, schema, migration, table, column, index, dependency,
route, or published response field was added, removed, or changed. The only
public response-shape change is the new capability failure status and code on
the compact endpoint.

## 4. Verification executed

Commands run from the repository root.

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo test --manifest-path rust/Cargo.toml --test remote_compaction_capability -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml -p eggpool-provider-profile --test contract -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
uv run pytest tests/tooling/ -q --tb=short
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run python scripts/validate_release_docs.py
uv run python scripts/validate_release_workflow.py
```

Targeted results: `remote_compaction_capability` 9 passed, 0 failed;
`eggpool-provider-profile` `contract` 15 passed, 0 failed. The full serial
workspace suite is 90 test binaries with 1164 passed, 0 failed, and 1 ignored.
The tooling suite is 245 passed and 1 skipped. Both strict Clippy
configurations, the `--no-default-features` check, `cargo fmt --check`, Ruff
format, Ruff lint, and Pyright are clean. The release-document validator and
the release-workflow validator both pass. The `--no-default-features` test
suite was not run; only the check and both Clippy configurations were.

The four corrective closure records opened in the same pass — Operations CLI
M003, Routing Selection M007, Request Admission and Wire M009, and Deployment
and Packaging M007 — each list "the full serial Rust workspace suite not run"
as an outstanding finding. That finding is discharged by the suite run above,
which covers every change those records closed over, because all of them
shipped in the same commit range. Those records are closed and immutable, so
they are not rewritten; this record carries the suite evidence for the
accumulated tree.

Regression-guard perturbation evidence. Each perturbation was a deliberate
revert applied to a copy-backed file and restored afterwards.

| Perturbation | Observed failure |
|---|---|
| The installation-level capability pre-check removed from `execute_input` | `a_missing_capability_is_reported_even_when_no_account_is_routable` FAILED — 8 passed, 1 failed |
| The route-level capability-miss recording removed | `a_routable_provider_without_the_capability_reports_the_capability_outcome` FAILED — 8 passed, 1 failed |
| The stable capability error code removed from `endpoint_error_body_for` | `the_capability_outcome_is_distinguishable_from_the_selection_failure` FAILED — 8 passed, 1 failed |
| The streaming coordinator's response context made to attach compaction capabilities | `the_streaming_coordinator_carries_no_compaction_capabilities` FAILED — 8 passed, 1 failed |
| A remote-compaction opt-in added to the bundled provider-profile asset | `embedded_data_advertises_no_remote_compaction_capability` FAILED — 14 passed, 1 failed |

The first perturbation also shows that the two capability mechanisms overlap
for an installation whose only provider is incapable: removing the
installation-level pre-check alone still yields the typed outcome through the
route-level miss. The pre-check's own contribution is that the outcome does
not depend on an account being routable, and the third test above is the one
that pins it.

## 5. Invariant review

- Routing is unchanged. The capability check reads the generation's provider
  and profile tables; it does not select, score, exclude, or re-rank a
  provider or an account, and it does not introduce a selection preference.
- The selector does not pin an account. The installation-level check runs
  before any account is chosen.
- No `.await` was introduced inside `RoutingRouter::selection_lock`, and no
  new lock, mutex, or concurrency primitive was added.
- The compact operation remains finite-only. The streaming coordinator's
  context construction was not changed, and the finite-versus-streaming
  response-context asymmetry is preserved deliberately.
- The wire runtime context for the finite path still resolves the compaction
  capabilities from the provider-owned table, so trigger validation and
  compact decoding observe the same operator facts as attempt preparation.
- The bundled provider-profile asset remains the single editable source of
  provider-profile facts, and it advertises no remote-compaction capability.
- The persisted vocabulary, the durable schema, the migration chain, and the
  failure-reason vocabulary are unchanged.
- No secret, credential, prompt, raw body, or cache key was added to
  persistence, logs, or diagnostics.

## 6. Failure and recovery review

- The capability outcome is a terminal, deterministic answer: the installation
  either exposes an eligible provider surface that advertises the capability
  or it does not. There is no retry, no backoff, no alternative provider
  fallback, and no translated compaction.
- The capability check is fail-closed with respect to the operator facts: a
  surface that advertises the flag without a compact path is not a native v1
  capability and is not eligible.
- A capability failure leaves the generation unchanged. No claim is held, no
  attempt exists, no quota reservation exists, and no durable attribution row
  exists, so a later request is unaffected.
- The route-level miss rolls the claim back before excluding the account, so
  the account's claim accounting is restored and the account is not silently
  starved by the miss.
- An upstream failure after a successful capability match keeps the ordinary
  upstream-failure envelope; the capability outcome is only reported when no
  dispatch ever reached an upstream.
- A reload that adds or removes the capability changes the answer at the next
  generation build. No restart is required, and no new reload classification
  was introduced.

## 7. Migration and compatibility review

- No schema change, no migration, and no table, column, or index change.
- No dependency change; `Cargo.toml` and `Cargo.lock` are unchanged.
- The only public response-shape change is the compact endpoint's capability
  failure: `501` with the code `remote_compaction_unsupported`. Clients that
  branch on the status code must be updated, and `docs/api-reference.md` says
  so.
- The compact endpoint's success, admission, stateless, and finite-only
  contracts are unchanged.
- A caller that previously treated the compact failure as `503` now sees
  `501`. The failure was previously indistinguishable from a selection
  failure, so no caller could have branched on it meaningfully.
- The bundled provider profile's published facts are unchanged.

## 8. Security review

- The capability failure body names only `supports_remote_compaction_v1` and
  `compact_path_template`. It carries no account name, credential material,
  provider secret configuration, provider identifier, model identifier, or
  request body material.
- The guard asserts the absence of a seeded credential marker, a seeded
  account marker, the provider identifier, the account identifier, the model
  identifier, and the `sk-` credential prefix in the rendered body.
- The check reads operator configuration only. It never reads a credential
  store, a secret reference, or a request body.
- The compact request body is not echoed, logged, persisted, or included in
  the capability failure.
- The capability outcome does not reveal which providers were inspected, how
  many were configured, or why any particular provider was skipped.
- The bundled asset guard asserts that the shared contract carries no
  compaction key, so no capability claim can enter it silently.

## 9. Documentation and operations

- `docs/api-reference.md` — the compact endpoint section replaces the
  forward-compatibility phrasing with the actual capability contract: the two
  configuration keys, the `501` status, the stable error code, the failure
  semantics, the distinction from the ordinary selection failure, and the
  guarantee that a capability failure consumes no provider attempt, no quota
  reservation, and no durable attribution row. The endpoint table row records
  the new failure.
- `architecture/deep-dive-transcoder.md` — records the capability contract,
  the two capability mechanisms, the content bound of the failure body, and
  the finite-only asymmetry between the finite and streaming response
  contexts.
- `plans/adrs/ADR-0003-remote-compaction-capability-contract.md` — the
  decision record is accepted; its body is unchanged apart from the status
  transition and the acceptance note.
- The operator action is a configuration change: set
  `supports_remote_compaction_v1 = true` and `compact_path_template` on the
  provider's `openai_responses` wire surface entry.

## 10. Unresolved findings

| Finding | Severity | Disposition |
|---|---|---|
| The `--no-default-features` test suite was not run; only the check and both Clippy configurations were. | Medium | Outstanding. The no-default feature set keeps the direct and non-SSH proxy paths and rejects SSH proxy configuration; the compact capability path does not depend on the SSH feature. |
| The account-scoped durable writer still emits a `model_unavailable` row with no model id for a model-scoped reason; the durable validation rejects that shape, so the write fails and is warned about. | Low | Carried from the routing corrective pass. A separate change. |
| `rust/src/health/effects.rs`'s `HealthEffectApplier` is a parallel, unused implementation of the same effect. | Low | Pre-existing. No production caller. |
| The dashboard parity harness is not runnable locally; it needs an external pinned Python oracle worktree. | Low | Pre-existing. The recorded M012 baseline shows 200/401 mismatches per route. |
| The installer-harness flake `tests/tooling/test_installer.py::test_quick_installer_harness_passes` is timing-sensitive under load. | Low | Pre-existing. It reproduces on the unmodified baseline. |

No unresolved finding blocks this milestone.

## 11. Roadmap disposition

`plans/subsystems/request-admission-wire-roadmap.md` records M008 as closed
and registers no successor in the subsystem. M001–M007 and M009 remain closed
through their own records; M007 is closed through M009.

The milestone's hard dependency on ADR-0003 is discharged: the decision record
is accepted and the capability contract it defines is implemented.

## 12. Registry updates

- `plans/registry.md`
  - The "Active subsystem roadmaps" row for "Request admission and wire" now
    reads `M008 closed — remote compaction capability contract`.
  - The "Blocked work" row for M008 is removed; the milestone was blocked on
    ADR-0003, which is accepted.
  - The "Recently closed" table gains the M008 row pointing at
    `plans/closure/request-admission-wire/008-status.md`.
  - The "Most recently closed" prose leads with M008.
- `plans/subsystems/request-admission-wire-roadmap.md` — the M008 row moves
  from `blocked` to `closed` and the milestone table records the closure.
- `plans/adrs/ADR-0003-remote-compaction-capability-contract.md` — the status
  moves from `proposed` to `accepted`.
- No other registry row changes.
