# On-Prem User Access M014 — Rustls advisory remediation and transport qualification

Status: ready

Repository baseline: `3c7def1e35871209e68563b2ee88f36033aff76e` on `plans/on-prem-small-team-user-access` (PR #7, open; 134-file implementation, schema 62).

Source roadmap:
- `plans/subsystems/on-prem-user-access-roadmap.md#m014`

Long-term requirements:
- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#2-end-state-invariants-normative`
- `plans/001-terminology-and-domain-model.md#identity-and-lifecycle-terms`
- `plans/002-long-term-roadmap.md#phase-5--optional-on-prem-small-team-access-and-budgeting`

Applicable ADR:
- `plans/adrs/ADR-0002-on-prem-user-identity-and-policy-boundaries.md` (accepted; no new ADR needed unless an auth or lease boundary must change).

Primary class: invariant

## 1. Objective

Restore a clean mandatory dependency-security gate on the on-prem integration
branch by resolving all four newly reported RustSec advisories against
`rustls 0.23.45`, with the **smallest lockfile-compatible TLS patch bump**
and transport/feature regression proof. Avoid unrelated provider-family
upgrades or cargo-deny exceptions. This is a merge prerequisite, but
does not assert an unverified exploit path in EggPool.

## 2. Why this milestone is ready

Hard dependencies: none beyond accepted provider-transport and M001–M013
contracts. `Cargo.toml` permits `rustls 0.23`; the resolved lockfile
is still 0.23.45 in both `main` and PR #7. The GitHub audit log itself
identifies the minimum fixed version (>=0.23.46). No hardware is needed
to make or test the patch; hosted security audit is the closure gate.
This issue also affects **main**; implement on PR #7 for merge
qualification and coordinate equivalent upstream/main resolution if it is
not merged promptly.

## 3. Current implementation evidence and missed verification

PR #7 head `3c7def1e35871209e68563b2ee88f36033aff76e`: GitHub Actions check and Linux/macOS installer
jobs pass, but `Dependency audit` run 38647245245/job 116137044959
fails. Its `cargo-deny` output contains **four denial-level advisories**
for resolved `rustls v0.23.45`:
- `RUSTSEC-2026-0346` — key-bearing `Debug` output from TLS session values.
- `RUSTSEC-2026-0347` — ECH rejection buffered data handling.
- `RUSTSEC-2026-0348` — Brotli certificate decompression allocation risk.
- `RUSTSEC-2026-0349` — ServerHello downgrade-related behavior.

The audit recommends `rustls >=0.23.46`. Separate duplicate-version
warnings are not what fails the gate. Direct `rust/Cargo.toml`
dependencies include `rustls`, `hyper-rustls`, `tokio-rustls`,
`eggfetch-core =0.2.2` and `eggress-outbound =1.0.12`;
transitives share the resolved Rustls family. `deny.toml` intentionally
rejects advisories without a blanket ignore. Earlier CI was green
**before** publication of the new advisories, so green Rust tests
never meant current security-audit clearance.

## 4. Invariants that must not regress

- EggPool remains a **single on-prem instance** for an owner or small team,
  with provider topology, upstream credentials, TLS trust, private LLM
  endpoints and routing configuration controlled by operator TOML/environment;
  user identities, profiles, sessions, audit and downstream quotas stay in
  SQLite. No SaaS, org/tenant control plane, embedded reverse proxy, external
  IAM, secret-bearing HTML or browser provider editor.
- Preserve generation leases, provider health/eligibility, request identity,
  retry and streaming finalization, single SQLite gate/worker and WAL/NORMAL,
  live/restart config authority, all supported Chat/Responses/compact/Messages
  client and cross-protocol routes, and the canonical solo SBC installation.
- Authorization and budget failure must not become provider unhealthiness
  or anonymous fallback. No unauthenticated web mutation, shared
  `admin/admin` credential, bypass of profile/global/model denies, or
  moving operator secrets to SQLite.
- All qualification uses fake provider tokens, temporary roots and redacted
  artifacts; never production data or real user passwords.

## 5. Scope

### In scope

- Capture current lockfile resolution and resolved feature graph.
- Qualify a minimal security-fixed `rustls 0.23.x` patch in the existing
  MSRV/toolchain constraints; preferred minimum 0.23.46 or a later safe
  compatible patch (select from actual crates.io graph at execution).
- Regenerate only necessary `rust/Cargo.lock` entries; review and
  justify every changed transitive (including `rustls-webpki` if forced).
- Verify TLS certificate validation, provider transports and feature matrix,
  and the actual hosted cargo-deny result against the same final SHA.
- Reconcile branch/main common vulnerability without mixing unrelated
  provider adoption.

### Explicitly out of scope

No upgrading Eggress 1.0.12/Eggfetch 0.2.2/EggServe 0.4.0 solely for
new features, no `deny.toml` advisory ignore/skip, no disabling TLS
verification, ECH flags or Brotli as a substitute for patching, no
new TLS architecture, no SQLite/identity changes, no unrelated lockfile
freshening, and no claim to security compliance certification.

## 6. Required production changes

Prefer lockfile-only resolution of the existing semver `rustls = "0.23"`
requirement. Inspect with `cargo tree --manifest-path rust/Cargo.toml -i
rustls --locked` (or explicitly select duplicate packages if needed)
and patch using:
```bash
cargo update --manifest-path rust/Cargo.toml -p rustls --precise 0.23.46
```
If 0.23.46 cannot resolve compatibly, choose the newest compatible
non-vulnerable 0.23.x patch and **record the reason and exact version**;
do not force an ABI/API-breaking major upgrade to make the gate green.
Run the audit against all features and the entire source lock graph,
not just default runtime. Ensure no second affected `rustls` copy or
known flagged `webpki` dependency remains. If feature unification alters
TLS client trust, SNI, TLS1.2/1.3, proxy or stream semantics, add the
narrowest fix and fixture for the regression, not another transport
backend.

## 7. Ordered work packages

A. Lock/advisory inventory — record all four IDs, original/fixed graph,
   reverse dependents, deny policy and reproducible failing command.

B. Minimal patch — update resolved Rustls, inspect lock diff, run
   `cargo tree -e features` and ensure permitted licenses/sources/MSRV.

C. Qualification — focused TLS and transcode routes, certificate
   negative cases, proxy transport, streaming and cancellation; whole
   default/no-default Rust plus Python tooling/installer matrix. Preserve
   native Q007 messages-to-responses bridge regression (M013).

D. Hosted verification — push candidate, require GitHub Actions CI,
   both installer jobs, and `cargo-deny` green **on new SHA**. Record
   exact advisory-free audit and a cross-branch note for the same issue
   still present in `main` prior to merge.

## 8. Failure, cancellation, restart and contention semantics

Provider TLS handshake, certificate rejection, upstream proxy/connect,
and truncated-body failure must stay in their existing typed error classes.
Do not log Rustls session `Debug` fields or other key material. No
streaming framing/content or retries change. TLS client state remains
generation-scoped where currently owned. A failed dependency resolution
must leave the committed lock graph unchanged. No schema/DB migration.
If a patched version changes provider trust behavior or features, stop
and register a transport-owned corrective, not a silent security bypass.

## 9. Compatibility and migration

No TOML keys, downstream credential types, binary installer semantics,
API/wire/DB changes expected. Preserve release reproducibility with
`--locked`. Both `main` and PR #7 share the existing vulnerable
resolution; coordinate the permanent fix once and avoid a fresh
lockfile conflict at merge. Published upstream crate pins remain
unchanged unless dependency solving proves that impossible, in which
case stop for a separate authorization decision.

## 10. Required tests

Focused:
```bash
cargo tree --manifest-path rust/Cargo.toml -i rustls --locked
cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_budgets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_global_budgets -- --test-threads=1
uv run pytest tests/tooling/test_qualification_live_provider.py -q
```
Inspect actual test-target inventory and substitute existing target
names if `provider_transport` is not a binary; do not claim a
nonexistent command passed. Add/extend TLS fixture tests for
valid CA, invalid/self-signed cert, hostname/SNI mismatch, and
proxy/transcode streaming EOF where missing. Include `cargo deny check
advisories` with actual repository root policy and lockfile selection.

## 11. Required verification commands

Record commands/results **actually executed** at the implementation SHA:
```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
uv sync --frozen
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
```
Use `cargo deny check` with the repository's root `deny.toml`
and verify the command/working directory that the GitHub audit workflow
actually invokes. Run `cargo build --manifest-path rust/Cargo.toml --locked
--release` for production-affecting Cargo changes. The hosted CI,
installer-shell and dependency-audit jobs must be green on the final
head SHA; no earlier green check is a substitute.

## 12. Documentation updates

Update `plans/registry.md`, `plans/subsystems/on-prem-user-access-roadmap.md`,
new closure record, and `architecture/deep-dive-security.md` or
`architecture/deep-dive-provider-transport.md` **only** if dependency
qualification reveals documentation drift. No upstream provider setup
changes are expected.

## 13. Acceptance criteria

At final candidate SHA, `Cargo.lock` resolves a patched Rustls family
with none of RUSTSEC-2026-0346/0347/0348/0349 denied, and the **actual**
hosted Dependency audit passes. Default/no-default and TLS negative/positive
transport, Q007 transcode, provider-retry/stream/installer tests pass.
No blanket deny exception or changed certificate-validation posture;
reproducible locked release builds and preserved SBC resource posture
within existing documented bounds.

## 14. Stop conditions

Stop on resolver incompatibility, MSRV violation, a forced unrelated
family upgrade, unstable TLS feature graph, upstream verification
regression, lack of actual hosted audit proof, or any remaining
denial-level security alert. Register a separate minimal upstream
dependency fix instead of widening this pass.

## 15. Closure evidence required

`plans/closure/on-prem-user-access/014-status.md`: before/after
lockfile and reverse dependency tree, all four advisory IDs, exact
`cargo-deny` output, verified patched version, test/feature/locked
release result, hosted job URLs/SHA, negative TLS fixtures, separate
main-branch coordination status, risk review, and registry unblock
audit (promote M016 only after M014 + M015 accepted closure).

## 16. Handoff notes

This is a repository-wide security dependency issue observed during
on-prem PR review, **not** evidence the new auth code caused the
Rustls vulnerability. Leave provider family pins and owner TOML alone.
