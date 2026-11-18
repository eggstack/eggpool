# Security Hardening Milestone 008 — russh advisory resolution

Status: blocked

Repository baseline: `dcbd820e9b978d47bd43d237eded1f488d3449ee`

Source roadmap:

- `plans/subsystems/security-hardening-roadmap.md#milestone-8--russh-advisory-resolution`

Long-term requirements:

- `plans/000-long-term-specification.md#2-end-state-invariants-normative` —
  item 10, no-default build parity and the SSH proxy boundary
- `plans/000-long-term-specification.md#3-ownership-boundaries-normative`
- `plans/002-long-term-roadmap.md#phase-1--transport-and-admission-hardening-sustaining`

Applicable ADRs:

- `plans/adrs/ADR-0001-binary-first-quick-install-authority.md` constrains the
  release artifact and installer behavior, not this decision.
- A new ADR is required before vendoring a patched `russh` or before changing
  the default `ssh` feature policy. Both are durable dependency/feature
  decisions.
- The existing `deny.toml` exception for RUSTSEC-2023-0071 is not a precedent
  for adding another exception.

Primary class: invariant

## 1. Objective

Return `cargo deny check advisories` to green without adding an advisory
exception, by moving the Eggress/`russh` stack to a release that contains the
client channel fix and the agent-confirmation fix, or by an explicit policy
decision that removes the affected feature from shipped artifacts.

## 2. Why this milestone is ready

Not ready at this baseline; the milestone is blocked on the external dependency
or ADR named in §14. The evidence below is why the plan is registered rather
than handed off.

- `rust/Cargo.toml` pins `eggress-outbound`, `eggress-server`,
  `eggress-core`, `eggress-config`, and `eggress-pproxy-compat` to `=1.0.12`.
- `eggress-transport-ssh 1.0.12` declares `russh = "0.62.6"` (a caret
  requirement that excludes `0.63.x` and `0.65.x`).
- `rust/Cargo.lock` resolves `russh 0.62.7`.
- `cargo deny` fails on RUSTSEC-2026-0492 (fixed in `russh >=0.63.1`) and
  RUSTSEC-2026-0495 (fixed in `russh >=0.65.0`). `cargo audit` reports the same
  two, plus the accepted `rsa` advisory and two warnings.
- The crates.io `eggress-outbound` release is still `1.0.12`; no published
  Eggress release permits a patched `russh`.
- The repository cannot force a compatible `russh` with `cargo update`:
  `cargo update -p russh --precise 0.65.2` is rejected by the Eggress
  requirement.

Reachability, stated honestly: EggPool uses Eggress as an SSH client and calls
`build_chain_executor`; it does not run an SSH agent server, so
RUSTSEC-2026-0495 is not reachable in this process. RUSTSEC-2026-0492 is
reachable only when an account uses an SSH proxy, and Eggress's default
`KnownHosts` policy rejects unknown or failed host keys, so exploitation
requires a compromised proxy endpoint or a poisoned `known_hosts`. The gate
still fails and the shipped default feature set contains the code.

## 3. Current implementation evidence

- `rust/Cargo.toml` — `default = ["ssh"]`; `ssh` enables
  `eggress-outbound/ssh` and `eggress-pproxy-compat/ssh`; exact Eggress pins.
- `rust/Cargo.lock` — `russh 0.62.7`, `russh-util 0.52.0`, `chrono 0.4.45`
  (the unmaintained warning), and `rsa 0.10.0-rc.18` under `russh`'s `rsa`
  feature.
- `deny.toml` — `unmaintained = "workspace"` hides the transitive `chrono`
  warning; the only ignored advisory is RUSTSEC-2023-0071 with a review date of
  2026-12-31.
- `.github/workflows/dependency-audit.yml` — runs `cargo deny check
  advisories bans licenses sources` on manifest changes and weekly.
- `rust/src/providers/transport.rs` — production dialing uses
  `eggress_outbound::OutboundConnector`; `eggress_server::build_chain_executor`
  is called with no SSH session cache.
- `--no-default-features` builds keep the direct/non-SSH proxy path and reject
  SSH proxy configuration before dialing.

## 4. Invariants that must not regress

- The advisory gate runs every declared feature, and no new exception is
  added.
- The SSH proxy path either works under the new pin or fails with the existing
  typed configuration error; it never falls back silently.
- `--no-default-features` still compiles, clippies, and tests.
- Provider transport ownership stays with Eggress; EggPool does not regain an
  SSH executor.
- The `rsa` exception is re-reviewed, not blindly carried; if the new stack
  removes RSA or the reachability changes, the exception is removed in the
  same change.
- Release binaries do not ship a feature the release notes say is absent.

## 5. Scope

### In scope (after unblocking)

- One of the three accepted paths:
  1. update the Eggress pin to a release that permits `russh >=0.65` and
     refresh the lockfile;
  2. vendor a patched `russh 0.62.x` and add a `[patch.crates-io]` entry with a
     documented removal condition, after its ADR;
  3. remove the default `ssh` feature from shipped artifacts and reject SSH
     proxy configuration, after its ADR and release-policy change.
- Requalification: `cargo deny`, `cargo audit`, the serial suite, the no-default
  guard, the locked release build, and an SSH proxy smoke test where available.
- `deny.toml` reason review and documentation updates.

### Explicitly out of scope

- Unrelated dependency refreshes in the same change.
- Adding a second SSH implementation or a fallback executor.
- Raising the advisory gate's ignore list.
- Changing the `rsa` exception without evidence.

## 6. Required production changes

Path 1 (preferred): bump the five Eggress pins together, `cargo update` the
transitive stack, confirm `russh >=0.65`, and adjust any Eggress API drift.
Path 2: add the patch entry and the vendored source, record provenance and the
removal condition, and keep the exact pin.
Path 3: change the default feature set, keep `ssh` as an explicit opt-in or
remove it, reject `ssh://` proxy URLs with the existing typed error, and
update the release notes and support matrix.

No schema or configuration shape changes beyond the feature-policy path.

## 7. Ordered work packages

### Work package A — Confirm the unblock

Intent: know which path is available before editing.

Required changes: check the latest Eggress release and its `russh`
requirement; check whether `russh 0.63.1`/`0.65.x` satisfy the workspace MSRV;
record the result.

Acceptance evidence: crates.io metadata, `cargo info`, and a `cargo update
--dry-run` transcript in the closure record.

### Work package B — Apply the selected path

Intent: one dependency or policy change with a clean diff.

Required changes: per path. For path 1, update the exact pins and lockfile
only. For path 2, add the patch and vendor tree with the ADR reference. For
path 3, change features and the typed rejection.

Acceptance evidence: `cargo tree -i russh` shows a patched version or no
`russh`; the lockfile diff is limited to the intended stack.

### Work package C — Requalify

Intent: prove the transport still works and the gate is green.

Required changes: run the gate, the serial suite, the no-default checks, the
locked release build, and an SSH proxy smoke test when a fixture is available.

Acceptance evidence: `cargo deny check advisories` exits 0 with no new ignore;
`cargo audit` no longer reports the two advisories; the SSH path is either
qualified or explicitly unsupported.

### Work package D — Documentation and policy

Intent: keep operators and future maintainers honest.

Required changes: update the dependency-policy notes, the support matrix, and
the `deny.toml` review comment; if path 2 or 3 is chosen, the ADR is the
authority.

Acceptance evidence: docs match the shipped feature set; the weekly audit job
passes.

## 8. Failure, cancellation, restart, contention semantics

Dependency resolution fails closed: if the Eggress requirement still excludes
the patched `russh`, the change does not land. An SSH proxy configured under a
no-SSH build must fail at configuration validation before dialing. A vendored
patch with a checksum mismatch fails the build, not a request.

## 9. Compatibility and migration

Path 1 is a dependency refresh with no user-visible API change. Path 2 keeps
the Eggress API and replaces one transitive crate; it must be removed when
Eggress permits the upstream fix. Path 3 removes a supported capability from
default artifacts and requires a release note, a support-matrix change, and a
migration note for operators using SSH proxies.

## 10. Required tests

- `cargo deny check advisories bans licenses sources`.
- `cargo audit --file rust/Cargo.lock`.
- `cargo tree -i russh` and `cargo tree -e features` for the SSH stack.
- `rust/tests/provider_transport.rs` — dialing and typed failure behavior.
- `cargo check/clippy --no-default-features` and the full serial suite.
- `cargo build --locked --release`.
- SSH proxy smoke test against a disposable endpoint when one is available.

## 11. Required verification commands

```bash
cargo deny --manifest-path rust/Cargo.toml check advisories bans licenses sources
cargo audit --file rust/Cargo.lock
cargo tree --manifest-path rust/Cargo.toml -i russh
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
```

## 12. Documentation updates

- `architecture/deep-dive-providers.md` — the SSH transport pin and the
  no-default boundary if the feature policy changes.
- `docs/providers.md` / `docs/deployment.md` — SSH proxy support statement.
- `deny.toml` — the `rsa` reason and review date, or its removal.
- `CHANGELOG.md` only if the feature policy changes.
- `plans/registry.md` — blocked-to-ready and closure transitions.

## 13. Acceptance criteria

- The advisory gate passes with no new ignore and `cargo audit` no longer
  reports RUSTSEC-2026-0492/0495.
- `cargo tree -i russh` shows `>=0.65` (path 1), a patched `0.62.x` with the
  ADR (path 2), or no `russh` (path 3).
- The SSH proxy path is qualified or explicitly rejected with the typed error.
- The `rsa` exception is either justified again on the new stack or removed.
- Default and no-default suites and the locked release build pass.

## 14. Stop conditions

Stop and report if:

- no Eggress release permits the patched `russh` and no ADR authorizes
  vendoring or a feature-policy change;
- the vendored patch cannot be tied to upstream commits with provenance;
- the SSH smoke test fails and the failure is not explained by a typed
  configuration error;
- the change expands into unrelated dependency updates;
- a maintainer proposes an advisory exception instead of a fix.

## 15. Closure evidence required

- The upstream release/requirement evidence.
- The dependency or feature diff and the lockfile delta.
- `cargo deny`/`cargo audit` output.
- The SSH path qualification or explicit unsupported statement.
- The `rsa` disposition.
- Release build and serial suite results.

## 16. Handoff notes

Do not run `cargo update` wholesale; the lockfile has unrelated pending
updates. Keep the exact Eggress pins in step. If vendoring, record the patch
provenance and the removal condition in the plan and the ADR; a vendored fork
must not become permanent by accident.
