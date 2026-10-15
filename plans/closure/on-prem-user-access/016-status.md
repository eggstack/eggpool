# On-Prem User Access M016 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/on-prem-user-access/016-final-pr7-merge-reconciliation.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-user-access-roadmap.md#m016--final-integration-and-merge-readiness-reconciliation`

Repository baseline reviewed: PR #7 base `main` at `9a0a903c9944b69a6c1ae9fa969bb8603d5c65d9`; runtime candidate `d8fd9c2a5cbcd4cda317a215e17e9a188699cb71`; M016 reconciliation head `3d68c715fdf5342cc36b46bef52e73dc1b9f5ba0` (planning-only commits over the same runtime tree).

## Implementation commits

- `7462263e` — M014 Rustls advisory corrective.
- `a526980c`, `deb1622f`, `d8fd9c2a` — M015 authz implementation and independent-review corrections.
- `d5683b1a` — close M015 and promote M016 to ready.
- `3d68c715` — activate M016 and reconcile PR #7 title/description.

## 1. Executive finding

M016 completed the final pre-merge reconciliation for PR #7 and recommends
**merge held pending maintainer review**. M014 and M015 are closed, local
default/no-default and tooling/browser/release checks pass, and hosted CI,
installer and dependency-audit checks pass on the exact M016 reconciliation
head `3d68c715fdf5342cc36b46bef52e73dc1b9f5ba0`. The runtime code at that
head is unchanged from the exact tested implementation candidate
`d8fd9c2a5cbcd4cda317a215e17e9a188699cb71`.

The hold is procedural: GitHub reports no submitted PR reviews
(`reviews: []`, `reviewDecision` empty). The independent M015 reviewer
confirmed its corrective findings were closed, but no maintainer has
submitted a formal PR review. PR #7 remains open and unmerged. This closure
records the completed recommendation; it does not approve or merge the PR.
The overall on-prem user-access roadmap remains active until a maintainer
decides whether to merge.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Freeze base/head and inspect the complete PR diff | PR #7; `gh pr view`; `gh api repos/eggstack/eggpool/compare/main...plans/on-prem-small-team-user-access` | pass | Base `main` at `9a0a903c`; tested head `3d68c715`; 78 commits ahead, 0 behind; 154 changed files, 40,350 additions and 331 deletions. |
| Reconcile append-only migrations from schema 54 | `rust/assets/db/migrations/0055_user_identity.sql` through `0062_instance_budgets.sql`; `checksums.json`; M010/M012 closure records | pass | Eight append-only migrations advance schema 54 through 62. M010 records a frozen schema-54 restore/forward migration to 62 with legacy history preserved; M012's physical artifact used schema 62. No M014–M016 migration was added. |
| Verify M014 advisory remediation and hosted audit | `plans/closure/on-prem-user-access/014-status.md`; audit [38721601318](https://github.com/eggstack/eggpool/actions/runs/38721601318) | pass | Rustls 0.23.46; no advisory ignore; exact M016-head audit passes. `main` remains on its old resolution until PR merge. |
| Verify M015 security findings and corrective behavior | `plans/closure/on-prem-user-access/015-status.md`; independent reviewer; scans `776c75d7` and `52bd79cc` | pass | Hard-cost proof reservation and managed browser-session access findings corrected and independently re-reviewed; no unresolved M015 security-correctness finding. |
| Run exact-head CI and tooling | CI [38721601336](https://github.com/eggstack/eggpool/actions/runs/38721601336) | pass | Exact PR head `3d68c715`; serial Rust suite, formatting, strict Clippy, no-default checks, Ruff, Pyright and tooling pytest all pass. |
| Run Linux/macOS installer shell qualification | Installer [38721601305](https://github.com/eggstack/eggpool/actions/runs/38721601305) | pass | Linux Bash and macOS zsh pass on exact PR head `3d68c715`. |
| Requalify dashboard and live-provider paths | Dashboard parity command and Q007 command below | pass | On-prem page reference: 70 observations / 22 screenshots; Q007 loopback fake: 7 passed. Historical-page parity remains the accepted M011 frozen-oracle result; no dashboard HTML changed in M014–M016. |
| Validate release docs and installed release executable | `scripts/validate_release_docs.py`; locked release build and isolated installed-binary execution | pass | Docs matrix validates release 0.8.2. Locally built release binary reports 0.8.2 and runs `--help`; SHA-256 recorded below. The quick-install script correctly rejects this Mac x86_64 host as unsupported for raw release installs. |
| Preserve measured Pi envelope and avoid unsupported claims | `docs/raspberry-pi.md` lines 237–272; M012 closure and artifacts | pass | Docs continue to distinguish 49 named user records from inference concurrency; 2–4 concurrent requests are the limited operating envelope, and 8-way saturates. No Pi performance plan or artifact was changed. |
| Reconcile PR metadata and report merge decision | PR #7 title/body; review API and `gh pr view` | pass | Title/body now reflect M014/M015 closure, current candidate and Pi envelope. Recommendation is merge held pending a maintainer PR review; PR remains open. |

## 3. Production implementation evidence

M016 made no runtime, dependency, schema, protocol or operator-config change.
The tested runtime candidate is `d8fd9c2a`; M016's subsequent commits only
update plans and PR metadata. The current PR head `3d68c715` has the same
runtime tree. The complete PR diff includes the on-prem feature, M014's
lockfile-only Rustls patch and M015's bounded security corrections.

Migration inventory is append-only: 0055 user identity, 0056 web sessions,
0057 request attribution, 0058 model policy, 0059 user budgets, 0060 admin
audit, 0061 budget enforcement and 0062 instance budgets. The migration
checksum manifest includes them. M010's schema-54 restore test is retained
as prior closure evidence; no downgrade migration is promised, and operators
must restore the matching pre-upgrade database backup to roll back.

The PR title is now “Implement on-prem small-team access — merge held for
maintainer review”. Its description says the work is on this branch
and not yet in `main`, links M014/M015 evidence, updates the measured Pi
envelope, and distinguishes 49 named users from concurrent inference.

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
uv run pytest tests/tooling/test_qualification_live_provider.py -q
uv run python scripts/validate_release_docs.py
uv run python scripts/qualification_dashboard_parity.py --skip-build --on-prem --screenshot-dir /tmp/m016-onprem-shots
git diff --check
```

### Results

- At runtime candidate `d8fd9c2a`: default workspace suite 1,024 passed / 1
  ignored; no-default workspace suite 1,025 passed / 1 ignored (79 suites).
  Focused auth/budget suites passed: sessions 10, user budgets 27, global
  budgets 16. The added hard-cost proof unit test and browser-session budget
  API integration test passed.
- Format, strict default Clippy, no-default check and strict no-default
  Clippy passed locally. Exact M016 head CI [38721601336] passed the full
  hosted Rust/tooling workflow, including serial default Rust tests, Ruff,
  Pyright and the full tooling suite.
- Exact M016 head installer run [38721601305] passed Linux Bash and macOS
  zsh. Exact M016 head dependency audit [38721601318] passed cargo-deny.
  The audit workflow invokes `.github/workflows/dependency-audit.yml` against
  `rust/Cargo.toml` with all features and checks advisories, bans, licenses
  and sources; no advisory exception was introduced.
- `uv run pytest tests/tooling/test_qualification_live_provider.py -q`:
  7 passed. `scripts/validate_release_docs.py`: pass for published 0.8.2 and
  Linux x86_64, Linux aarch64 and macOS arm64 targets. On-prem dashboard
  qualification: 70 observations and 22 screenshots passed against
  `tests/fixtures/dashboard-on-prem-browser/reference.json`.
- `cargo build --manifest-path rust/Cargo.toml --locked --release` passed.
  The release executable was copied to an isolated temporary install path;
  `eggpool version` returned `0.8.2` and `eggpool --help` completed. Binary
  SHA-256: `bc576809c7cf190393b6face4afbfde4fb25051bf3863f7777db606675b0e7e7`.
  The repository's raw installer was also invoked from the checkout and
  refused before install with `unsupported platform Darwin/x86_64`, as this
  host is outside the supported raw-release target matrix. The binary itself
  was independently installed and exercised in the temporary path.
- No release was packaged or published. Existing M014 locked release build,
  M011 historical Python-oracle parity (54 unchanged historical gaps), M010
  schema-54 restore evidence and M012 physical artifacts are referenced from
  their immutable closure records.

## 5. Invariant review

The base is current and the PR has no merge-base conflict (`behind_by: 0`).
Rustls advisory remediation is on the PR candidate and remains absent from
`main` until merge. M015 confirmed managed/legacy auth separation, member
budget isolation and proof-bounded hard-cost holds. The default and
no-default suites pass; no known route, model, quota, recovery, TLS,
transcode, or redaction regression remains. The user-access boundary remains
single-instance and local; there is no external IAM or provider-secret web
editor.

## 6. Failure, recovery and contention review

The M010 closure covers startup recovery of abandoned user/global budget
holds and no double-charge behavior. M015's integration matrix covers
configured-key managed browser sessions, member self-scope, cross-member
denial and admin instance-rule visibility. M014 retains wrong-root and
hostname-mismatch TLS rejection. M013 retains the Messages-to-Responses
eligibility regression guard. M012 and `docs/raspberry-pi.md` keep MMC tail
behavior explicit; no WAL or SQLite writer topology change is proposed.

## 7. Migration and compatibility review

Schema 54 to 62 uses immutable append-only migrations 0055–0062 and the
checksum manifest; restored-schema compatibility and legacy history are
covered by M010. M016 adds no migration. Current personal tokens, browser
sessions, legacy-key migration behavior, provider TOML, local model choices,
solo SBC install, client wire surfaces and explicit backup-based downgrade
procedure remain unchanged. The locked dependency graph passes hosted
cargo-deny and release build.

## 8. Security and reviewer disposition

M014 clears the four RustSec advisories by upgrading the resolved Rustls
family to 0.23.46 without suppression. M015's independent reviewer found and
closed the hard-cost reservation and browser-session authorization defects;
the final scoped security scan of `deb1622f..d8fd9c2a` completed with zero
reportable findings. Its earlier bounded scan did not find those issues, so
the independent reviewer evidence and corrective re-review are retained
explicitly in the M015 closure rather than overstating scan coverage.

GitHub currently reports `reviews: []` and an empty `reviewDecision`. No
maintainer PR review or approval has been submitted. This is the exact reason
for the **merge held** recommendation. The automated checks are green; no
code finding requires another corrective plan. A maintainer must review the
reconciled PR and decide whether to merge.

## 9. Documentation and operations

The PR body is reconciled with current implementation and M014–M015 status.
README, deployment, model-limit, backup and security architecture claims
required no source change: the on-prem implementation and M012 envelope
remain accurately described. The Pi documentation still states seconds-scale
MMC tails, limits practical concurrency to 2–4 requests, and says 49 named
records are not 49 simultaneous generations. The separate Pi latency plan
was not opened, edited or used as a blocker.

## 10. Unresolved findings and hold

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| Operational hold | No maintainer review has been submitted on PR #7 (`reviews: []`, `reviewDecision` empty). | The code and hosted checks are ready, but the requested final reviewer/maintainer gate is not satisfied; do not merge automatically. | Maintainer reviews PR #7 and independently decides whether to merge. No further implementation plan is required unless that review finds a new bounded defect. |

## 11. Roadmap disposition and unblock audit

M016 is closed as a completed pre-merge reconciliation with recommendation
**merge held pending maintainer review**. The on-prem user-access roadmap
remains active because PR #7 is open and its runtime changes are not in
`main`. A registry and dependency-graph search found no registered plan
blocked on M016; no future implementation plan can be promoted from this
closure. Persistence M011 remains independently ready. On-prem concurrency
performance M001 remains a separate Pi workstream and was explicitly excluded
from this work.

## 12. Registry updates

The registry records M014 and M015 closed, M016 implemented/closed with an
explicit merge hold, and no blocked on-prem successor. The on-prem roadmap
retains active status until the maintainer decision and PR integration. No
M001–M015 historical closure was rewritten, no Pi plan or artifact was
modified, and no merge was performed.
