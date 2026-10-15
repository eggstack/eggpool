# On-Prem User Access M017 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/on-prem-user-access/017-managed-client-credential-reconciliation.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-user-access-roadmap.md#m017--managed-client-credential-reconciliation`

Repository baseline reviewed: `88f58bbf2e9095cf46437945edf83e580270bdb6`

Implementation commits:

- `d726bce` — managed local integration setup requires an active personal credential; keyless remote export and obsolete credential guidance reconciled.
- `218df45` — explicit-credential and keyless-export regression coverage; remote instructions name personal tokens.
- `c5167aa` — `getkey`/`newkey` reject retired shared-key use after administrator initialization.

## 1. Executive finding

Managed local client setup no longer resolves or creates the retired shared
server key. It requires `EGGPOOL_API_KEY` to contain an active personal token
from the local credential store. `getkey` and `newkey` clearly refuse after
identity initialization. Remote configuration remains a read-only,
credential-free profile export and works without a configured server key.

The cross-component behavior is covered with a synthetic provider and managed
personal credentials. No real provider keys, user prompts, or third-party agent
runtime were used.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Legacy, fresh, initialized, and managed credential behavior stays separated | `rust/src/runtime.rs::integration_personal_credential`, `identity_is_initialized`, `getkey`, `newkey`; `rust/tests/on_prem_identity.rs`; `rust/tests/operations_o005.rs` | pass | Initialized installs verify the supplied token before rendering; legacy behavior remains before initialization. |
| Managed local setup does not resurrect a shared key | `explicit_personal_credential_does_not_resurrect_shared_server_key`; runtime managed credential resolution | pass | Config remains keyless and unchanged when an explicit personal credential is supplied. |
| Remote setup does not require or create a global key | `remote_context_is_read_only_for_keyless_managed_config`; `configremote_is_read_only_deterministic_and_secret_free` | pass | Config remains byte-identical; absent catalog DB is not created. |
| Profile/token output contains no credential | `operations_o005` profile assertions and `eggpool-connect` contract suite | pass | `epc1` carries endpoint/profile metadata and `EGGPOOL_API_KEY` remains a receiving-machine environment reference. |
| Inference, discovery, policy filtering, and revocation use personal identity | `rust/tests/on_prem_attribution.rs`, `rust/tests/on_prem_sessions.rs`, `rust/tests/on_prem_model_policy.rs` | pass | Synthetic local provider; unauthorized and revoked credentials are denied. |
| Documentation no longer directs managed users to `getkey` | `tests/tooling/test_on_prem_credential_guidance.py`; release-doc validator | pass | `getkey` is documented as legacy-only. |

## 3. Production implementation evidence

- `rust/src/operations/integrations.rs` accepts an explicit credential for
  local rendering and omits the global-key prerequisite from remote export.
- `rust/src/runtime.rs` detects initialized identity state, verifies the
  personal token before local setup, and refuses legacy key retrieval/rotation
  after bootstrap.
- README and integration guidance distinguish member tokens from the retired
  shared key.
- No database migration, credential plaintext persistence, or profile schema
  change was introduced.

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml -p eggpool-connect -- --test-threads=1
cargo test --manifest-path rust/crates/eggpool-client-config/Cargo.toml
cargo test --manifest-path rust/Cargo.toml --test operations_o005 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_identity -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_sessions -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_model_policy -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1
uv run pytest tests/tooling/test_on_prem_credential_guidance.py -q --tb=short
uv run python scripts/validate_release_docs.py
git diff --check
```

### Results

- Default local workspace: 1,028 passed, 1 ignored across 79 suites.
- No-default local workspace: 1,029 passed, 1 ignored across 79 suites.
- `eggpool-connect`: 59 passed; portable client-config: 49 passed.
- Focused post-implementation CLI, operations, identity, session, and model
  policy suites passed (6, 15, 3, 10, and 4 tests respectively); runtime unit
  target passed (4 tests).
- Strict Clippy passed with default and no-default features. Formatting,
  tooling regression guard, release-doc validation, and diff checks passed.
- These are local results. Hosted CI and release installers were not run in
  this closure pass.

## 5. Invariant review

- Provider secrets remain in provider configuration/environment; member
  credentials remain one-way digests in SQLite.
- Managed authentication uses the typed personal principal and does not fall
  back to a stale shared server key.
- Remote profiles remain secret-free and contain no user token.
- Existing legacy authentication remains available only before identity
  initialization; no new shared-key access path was introduced.

## 6. Failure and recovery review

Missing, empty, expired, revoked, or non-personal credentials fail before
client configuration is rendered. Remote export is read-only and tolerates an
absent catalog file. Existing personal-token revocation tests confirm that
subsequent authenticated requests fail. No new asynchronous worker or durable
state was added.

## 7. Migration and compatibility review

No migration or profile protocol change. `EGGPOOL_API_KEY` remains the client
environment variable. Existing pre-bootstrap `getkey`/`newkey` behavior is
preserved; initialized installations receive an actionable personal-token
message instead of a dead shared key.

## 8. Security review

Personal credentials are verified against the selected installation before
local setup. No token is logged or placed in the remote profile. The generated
Codex/OpenCode profile remains environment-variable based; secret-bearing
targets retain their existing protected delivery behavior.

## 9. Documentation and operations

Updated `README.md` and `architecture/deep-dive-integrations.md`; added a
tooling guard against the obsolete post-bootstrap `getkey` example. The
release-document validator passed.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| none | No unresolved M017 implementation finding | — | — |

## 11. Roadmap disposition

Milestone closed. Hard-budget compatibility M022 may proceed. M020 remains
blocked on M019; M021 remains blocked on M020; M023 remains blocked on M022
and ordinary-server operational evidence.

## 12. Registry updates

`plans/registry.md` records M017 closed, promotes M022 to ready, and includes
the dependency audit. The roadmap marks M017 closed and M022 ready. Historical
M001–M016 closure records were not changed.
