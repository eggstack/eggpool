# On-Prem User Access M014 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/on-prem-user-access/014-rustls-advisory-dependency-corrective.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-user-access-roadmap.md#m014--rustls-dependency-security-advisory-corrective`

Repository baseline reviewed: `6f43611e8fb4e0e1d5f216a3a0ca254b2c416519`; implementation candidate: `7462263ec482c5fda4bd1a70a9a55b0efc313cd3`.

## Implementation commits

- `7462263e` — update the locked Rustls family to 0.23.46 and add an untrusted-root provider TLS regression test.
- `80852e95` — mark M014 active before implementation.

## 1. Executive finding

M014 is complete on the PR #7 candidate. The lockfile resolves the single
Rustls package at 0.23.46, and no direct manifest, provider-family pin,
feature, or other lock entry changed. Default and no-default Rust suites,
provider TLS/transport, budget/coordinator tests, formatting, strict Clippy,
no-default checks, locked release build, and Python tooling passed locally.
The exact implementation SHA passed the hosted dependency audit, CI, and both
installer-shell jobs. No advisory exception was added.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Patch the full resolved Rustls family without unrelated upgrades | `rust/Cargo.lock`, `cargo tree -i rustls --locked`, `cargo tree -e features -i rustls --locked` | pass | Exactly one resolved `rustls 0.23.46`; reverse dependents and feature graph unchanged. |
| Resolve RUSTSEC-2026-0346/0347/0348/0349 without ignores | Hosted dependency audit run [38671345587](https://github.com/eggstack/eggpool/actions/runs/38671345587), job 116214950405 | pass | `cargo-deny` reports advisories, bans, licenses, and sources successful on candidate SHA `7462263e`. |
| Preserve TLS trust and failure behavior | `provider_transport`: trusted explicit root succeeds, unrelated root fails as `TransportError::Tls`, hostname mismatch fails as `TransportError::Tls` | pass | Focused tests and full provider transport target passed. |
| Preserve provider, proxy, retry, stream, budget, and transcode behavior | `server_transport`, `provider_transport`, `on_prem_budgets`, `on_prem_global_budgets`, coordinator C007/C008/C009/C011, boundaries, finalization, publication; full tooling suite | pass | The tooling suite includes `test_qualification_live_provider.py` (Q007). |
| Preserve default and no-default build/test behavior | Local full workspace suites and no-default check/Clippy; hosted CI run [38671345679](https://github.com/eggstack/eggpool/actions/runs/38671345679) | pass | Local default 1,009 passed/1 ignored; local no-default 1,010 passed/1 ignored. Hosted default suite and tooling also passed. |
| Preserve locked release and installer qualification | `cargo build --manifest-path rust/Cargo.toml --locked --release`; hosted installer run [38671345597](https://github.com/eggstack/eggpool/actions/runs/38671345597) | pass | Linux Bash and macOS zsh jobs passed on candidate SHA. |

## 3. Production implementation evidence

Only `rust/Cargo.lock` changed in the production dependency graph: `rustls`
0.23.45 became 0.23.46 with the corresponding registry checksum. The
Eggfetch, Eggress, Hyper-Rustls, and Tokio-Rustls versions and feature graph
remain unchanged. `rust/tests/provider_transport.rs` now also verifies that a
server signed by an unrelated root is rejected.

## 4. Verification executed

### Commands run

```bash
cargo tree --manifest-path rust/Cargo.toml -i rustls --locked
cargo tree --manifest-path rust/Cargo.toml -e features -i rustls --locked
cargo deny --manifest-path rust/Cargo.toml check advisories bans licenses sources
cargo test --manifest-path rust/Cargo.toml --test server_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_budgets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test on_prem_global_budgets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c007 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test provider_transport direct_https_rejects_a_server_signed_by_an_untrusted_root -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test provider_transport direct_https_uses_hostname_verified_explicit_test_root -- --test-threads=1
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
uv sync --frozen
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
```

### Results

- Local `cargo deny` from the repository root passed all four checks. It
  printed the repository's existing duplicate-version warnings; there were
  no advisory denials. The hosted dependency-audit workflow uses
  `.github/workflows/dependency-audit.yml`, `rust/Cargo.toml`, all features,
  and `check advisories bans licenses sources`; its exact-candidate job
  passed.
- Focused Rust suites passed: server transport 14, provider transport 36,
  user budgets 27, global budgets 16, C007 15, C008 34, C009 13, C011 18,
  coordinator boundaries 6, finalization 11, and publication 10 tests.
  Both focused TLS cases passed.
- Local full default suite: 1,009 passed, 1 ignored across 79 suites. Local
  full no-default suite: 1,010 passed, 1 ignored across 79 suites.
- Format, default Clippy, no-default check/Clippy, and locked release build
  passed. Tooling passed: Ruff, Pyright (0 errors/warnings/information), and
  pytest (187 passed, 1 skipped).
- Hosted CI run 38671345679 passed the default Rust suite and full tooling
  gates. Hosted installer run 38671345597 passed Linux Bash and macOS zsh.
  These jobs tested candidate SHA `7462263ec482c5fda4bd1a70a9a55b0efc313cd3`.

## 5. Invariant review

The TLS feature set, trust roots, SNI/hostname validation, proxy transport,
typed provider errors, streaming behavior, retries, and configuration are
unchanged. No credentials or session data enter diagnostics. No schema or
wire changes were made, and the reduced-feature path remains green.

## 6. Failure and recovery review

No lifecycle, persistence, cancellation, retry, or finalization behavior
changed. The new negative TLS case confirms an untrusted chain fails closed;
the existing hostname mismatch case remains fail-closed.

## 7. Migration and compatibility review

There is no database migration or configuration/API change. The lockfile is
reproducible with `--locked`; all affected Rustls dependents resolve to the
same patched version.

## 8. Security review

All four denial-level RustSec advisories are cleared by the patched package
on the candidate SHA. No `deny.toml` suppression was introduced. The
certificate validation posture remains unchanged and is covered by trusted,
untrusted-root, and hostname-mismatch fixtures. `main` still resolves 0.23.45
until PR #7 merges; the shared fix is ready on PR #7 and remains a merge
prerequisite.

## 9. Documentation and operations

The implementation and source roadmap now record Rustls 0.23.46 and the
exact-SHA closure evidence. No runtime or operator documentation changed.
M016 remains blocked on M015 and its own final-SHA integration evidence.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| medium | `main` retains Rustls 0.23.45 until PR #7 merges | The advisories remain present on `main` before integration. | Keep PR #7 as a merge prerequisite; M016 must reconcile the final candidate and must not claim the vulnerability is fixed on `main` before merge. |

## 11. Roadmap disposition

M014 is closed on PR #7's exact audited candidate. The only successor
dependency is M016, which remains blocked on M015's accepted closure and
final candidate integration checks. The separate Pi 5/MMC performance plan
has no dependency edge to M014–M016 and remains untouched.

## 12. Registry updates

`plans/registry.md` removes M014 from the ready/active tables, records this
closure, promotes M015 (whose hard predecessors are satisfied) as ready, and
retains M016 as blocked. The on-prem roadmap records M014 closed. The
on-prem concurrency-performance M001 status and evidence were not changed by
this closure.
