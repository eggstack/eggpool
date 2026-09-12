# Plan 184 — Dependency Security and Drift Automation

Date: 2026-09-12
Status: ready for handoff
Parent roadmap: `plans/178-rust-maintenance-consolidation-roadmap.md`
Planning baseline: `78a4da64de3c94a9f5fe29e05e6bdf40402bc16b`
Priority: P1/P2 supply-chain security / low-noise maintenance
Execution target: GPT-5.6 Luna/Sol or comparable implementation model

## Purpose

Add a small, explicit policy layer around EggPool's intentionally retained Rust dependency graph so known-vulnerability, license, and unexpected-source drift is detected automatically without reopening Plan 170's completed feature-minimization work or making every ordinary source commit slower.

The production graph is substantial because EggPool deliberately supports Eggress proxy compatibility, SSH, legacy proxy/crypto modes, Rustls/Hyper, bundled SQLite, archive/update behavior, and native packaging. The correct next step is continuous detection, not speculative crate removal.

## Research basis

RustSec is the Rust ecosystem advisory database consumed by both `cargo-audit` and `cargo-deny`. `cargo-deny` additionally supports license, source, banned-crate, and duplicate-version policy over Cargo metadata/lockfiles.

The official `EmbarkStudios/cargo-deny-action` accepts a Cargo manifest path and can run selected checks in GitHub Actions. Its documentation specifically notes that advisory publication can cause previously green code to fail without a repository change, so advisory scheduling/notification should be designed intentionally rather than blindly inserted into every source-only CI path.

GitHub Dependabot currently supports the Cargo and uv ecosystems as well as GitHub Actions. Automated version-update PRs are useful but are not required for this phase: EggPool has exact-pinned compatibility-sensitive Eggress crates, and low-noise vulnerability/policy detection is the immediate goal.

## Current-state findings

- `rust/Cargo.toml` has an intentionally explicit direct dependency/feature graph and `Cargo.lock` is committed.
- Plan 170 already traced direct dependencies/features to live owners and removed the only demonstrated unused direct dependency/feature at that time.
- the ordinary CI workflow runs format, strict Clippy, the full Rust suite, and retained Python tooling checks but no RustSec/license/source audit;
- no `.github/dependabot.yml` is currently present;
- the lockfile includes many legitimate duplicate/transitive families caused by networking, crypto, SSH, platform, and test support.

Therefore a policy that starts by denying all duplicate versions would create noise and undermine the purpose of this plan.

## Governing constraints

1. Do not remove or replace dependencies merely to make the policy file shorter.
2. Do not disable supported Eggress proxy URI/chaining, SSH, legacy compatibility, TLS 1.2, bundled SQLite, backup, updater/archive, or deterministic test behavior in this phase.
3. Do not treat all duplicate versions as errors. Report or selectively ban only dependencies with a concrete maintenance/security reason.
4. Do not create a broad advisory ignore list to make the first run green.
5. Every vulnerability/advisory ignore must identify the exact advisory/crate, why EggPool is not currently exploitable or cannot yet upgrade, and a review condition/date in an adjacent comment or maintenance record.
6. Keep source policy strict: unexpected git registries/sources should not silently enter the production graph.
7. Build a license allowlist from the actual resolved graph and EggPool's distribution obligations; do not copy a generic allowlist from another project.
8. This tooling is policy/engineering assistance, not legal advice. Ambiguous licenses require human review.
9. Prefer a separate lightweight dependency workflow triggered by manifest/lock changes plus a schedule/manual dispatch over adding network advisory fetches to every source-only CI run.
10. Do not add `cargo-auditable` or embed dependency inventories into the release binary unless a separate distribution/forensics requirement justifies the binary/build impact.
11. Do not enable automated dependency merging.

## Workstream A — Establish the current dependency/security baseline

Before writing policy, run locally from the planning implementation tree:

```bash
cargo metadata --manifest-path rust/Cargo.toml --format-version 1
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
cargo deny --manifest-path rust/Cargo.toml check advisories
cargo deny --manifest-path rust/Cargo.toml check licenses sources
```

Use the current `cargo-deny` CLI syntax/version actually selected by the implementation; verify flags rather than assuming old syntax.

Classify any findings into:

- vulnerable/unsound and upgradeable now;
- vulnerable but blocked by a compatibility/upstream constraint;
- unmaintained/yanked informational maintenance debt;
- license/source policy issue;
- benign duplicate-version noise.

Fix real upgradeable security findings before adding exceptions.

## Workstream B — Add a minimal `deny.toml`

Place one `deny.toml` at the repository root unless the selected action/tooling requires another canonical location.

Configure only policies the repository is prepared to maintain:

### Advisories

- use RustSec as the advisory authority;
- fail for actionable known vulnerabilities/unsoundness according to the current cargo-deny semantics selected during implementation;
- choose an explicit policy for unmaintained and yanked crates, preferably warning unless they create a concrete security/release issue;
- keep ignores empty unless the baseline produces a genuinely unavoidable advisory;
- any ignore must include a precise reason and review trigger.

### Sources

- deny unexpected registries and git sources;
- allow crates.io/current Cargo registry source forms required by the graph;
- allow the local path workspace member `eggpool-model-routing` naturally;
- add no broad GitHub organization wildcard unless an actual dependency requires it.

### Licenses

- generate/inspect the resolved production license set first;
- allow only licenses actually acceptable for EggPool distribution;
- use per-crate exceptions/clarifications when a dependency has a special expression rather than globally allowing a license solely for one crate;
- leave dev-dependency inclusion at a consciously chosen setting and document whether the gate represents shipped production code or the complete contributor/test graph.

### Bans/duplicates

Keep duplicate versions at warning/informational level initially. Add a targeted ban only where there is a concrete deprecated/insecure crate policy. Do not create a large skip list for common platform/crypto duplicates.

## Workstream C — Add a low-noise dependency audit workflow

Add a dedicated workflow, e.g. `.github/workflows/dependency-audit.yml`, with:

```text
triggers:
  pull_request/push when rust/Cargo.toml, rust/Cargo.lock, deny.toml,
  or the dependency workflow itself changes
  weekly schedule
  workflow_dispatch

job:
  checkout
  cargo-deny official action/tool
  manifest-path: rust/Cargo.toml
  checks: advisories + licenses + sources (+ configured bans)
```

Pin actions according to the repository's existing workflow policy/current reviewed release. Do not invent a permanent nightly matrix or cross-platform audit: dependency metadata/advisory policy does not require all supported runtime targets.

For scheduled runs, failure is useful signal and should be visible. For dependency-changing PRs, policy failures should block the dependency change once repository branch-protection policy consumes the check. Do not require this network-dependent audit to run on unrelated docs/source-only PRs.

If advisory database availability becomes flaky, use normal action/cache behavior first; do not silently mark vulnerability checks successful on network failure.

## Workstream D — Decide whether to add Dependabot configuration separately from the gate

The security gate does not require Dependabot version-update PRs. During implementation, inspect repository settings/maintainer preference:

- GitHub Dependabot alerts/security updates may complement RustSec and can use GitHub's advisory database;
- weekly Cargo version updates can be configured with `.github/dependabot.yml` if desired;
- uv and GitHub Actions are supported ecosystems if maintainers want those update PRs too.

For this maintenance line, **do not make routine version-update PR automation an acceptance criterion**. The exact-pinned Eggress stack and compatibility-sensitive networking graph benefit from deliberate upgrades with focused tests. If `dependabot.yml` is added, use a low open-PR limit and grouping/schedule that does not create update churn.

## Workstream E — Document the local developer command

Update `.opencode/skills/development/SKILL.md` and any concise contributor documentation with the chosen local command, for example:

```bash
cargo deny --manifest-path rust/Cargo.toml check
```

Keep Cargo tree commands as the feature/provenance authority. Make clear that passing cargo-deny does not replace strict Clippy/tests/provider-transport qualification for a dependency upgrade.

## Workstream F — Define the dependency-change qualification rule

When `rust/Cargo.toml` or `Cargo.lock` changes, require:

```bash
cargo deny --manifest-path rust/Cargo.toml check
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
```

For Eggress/Hyper/Rustls/Tokio/SQLite/archive changes, additionally run the focused owner tests appropriate to the changed component. Do not run live provider tests merely because a lockfile changed.

## Required verification

After policy/workflow creation:

```bash
cargo deny --manifest-path rust/Cargo.toml check
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
git diff --check
```

Validate the workflow YAML using the repository's existing workflow validator/release tooling if available. Trigger the dependency-audit workflow manually once after merge or inspect its first dependency-path run before considering the phase closed.

## Acceptance criteria

- A reviewed `deny.toml` covers RustSec advisories, licenses, and dependency sources for the Rust workspace.
- No known actionable vulnerability is ignored merely to obtain a green baseline.
- exceptions are precise, justified, and reviewable.
- legitimate duplicate versions are not converted into high-noise CI failures.
- dependency policy runs automatically on Rust dependency changes and on a bounded schedule/manual trigger.
- ordinary source-only CI does not gain a large network-dependent delay.
- contributor/development guidance includes the local dependency audit command.
- Plan 170's supported dependency/features remain intact unless an actual security fix independently requires a qualified upgrade.

## Handoff note

The goal is early detection with low maintenance noise. A small policy that maintainers trust is better than a maximal deny file full of exceptions. Treat any dependency upgrade exposed by this work as its own tested change, especially for Eggress, SSH/crypto, TLS, SQLite, and archive/update paths.