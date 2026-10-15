# On-Prem User Access Milestone 013 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/on-prem-user-access/013-transcode-eligibility-corrective.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-user-access-roadmap.md`

Repository baseline reviewed: `4699ce11` (M013 implementation)

Implementation commits or pull requests:

- `4699ce11` — Restore transcode protocols on the admission routing bridge
- `f704af27` — Activate on-prem user access M013 (planning status only)

## 1. Executive finding

Hosted CI on the M008/M009 push failed the Q007 loopback tooling gate while
every Rust gate passed. Bisection isolated the break to M008 implementation
`0149a6c8` (its parent `b0e8715f` passes all seven qualification cases):
the admission-bridge edit had dropped the adjacent
`facts.transcode_protocols = inputs.transcode_protocols.clone();` line, so
admission-built routing facts carried an empty transcode list and
cross-surface candidates lost eligibility (`messages-to-responses-finite`
→ 503, native surfaces unaffected). The one-line restoration plus a bridge
regression guard closes the corrective. No budget, schema, config, wire, or
operator-surface behavior changed; the M008 contract is preserved byte for
byte apart from the restored copy.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Restore the deleted transcode-protocol copy (§5) | `rust/src/request/admission.rs::routing_request_facts_from_parts` regains `facts.transcode_protocols = inputs.transcode_protocols.clone();`; `git diff` shows exactly one production line | pass | No other production line touched. |
| Bridge regression guard (§5) | `admission_preserves_routing_inputs_for_transcode_eligibility` in `rust/tests/on_prem_budgets.rs` asserts transcode/requested-protocol/catalog/capability/now/surface plus budget facts survive admission | pass | Verified fail-without (empty list) / pass-with in §4. |
| Q007 loopback passes (§12) | Local `run_qualification` on the fixed debug binary: all 7 cells 200, status pass; hosted-failing target `test_q007_loopback_fake_emits_secret_free_evidence` green inside the local tooling suite | pass | Same binary shape CI builds. |
| Removed-line audit (§3) | Full `rust/src/` removed-line review of `git diff b0e8715f..ea56ddcb`: every other removed line maps to an intentional M008/M009 restructure (evaluator rewrite, attempt-record extraction with verbatim move, index shifts, replaced UI strings) | pass | The transcode copy was the sole accidental deletion. |
| No scope expansion (§5/§13) | No migration, config, API, CLI, dashboard, or doc change; `git status` shows only the two touched files | pass | Plan + closure are the only planning artifacts. |

## 3. Production implementation evidence

One production line restored in `routing_request_facts_from_parts` plus one
regression test. Nothing else landed: no ownership, storage, protocol,
runtime, or operator change. The M008/M009 closures remain immutable and
accurate apart from the defect they could not see (addressed here, not by
rewriting them).

## 4. Verification executed

### Commands run

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
```

Bisection evidence (all local, same worktree procedure):

- Base `b0e8715f` debug binary → Q007 all 7 cells pass.
- M008 `0149a6c8` debug binary → `messages-to-responses-finite` 503, first three native cases pass.
- Fixed HEAD debug binary → Q007 status pass, all 7 cells 200.
- Guard proof: with the line temporarily removed, the new test fails
  (`left: []`, `right: ["openai", "anthropic", "gemini"]`); with it
  restored, the test passes.

### Results

Local (this machine, serial as required):

- Full default workspace/all-targets serial suite: 993 passed, 0 failed
  (992 carried + 1 bridge guard).
- Full no-default workspace/all-targets serial suite: 994 passed, 0 failed.
- Strict Clippy default + no-default, `fmt --check`, `git diff --check`:
  pass (one fmt/clippy round-trip on the new test folded into the
  implementation commit before verification).
- Tooling: `uv sync --frozen` clean, ruff format (59 files) + ruff check
  pass, pyright 0 errors, full `pytest tests/tooling/`: 166 passed,
  1 skipped — including the hosted-failing Q007 target.
- Hosted CI: the failing `check` run is `38517675228`; this push re-runs it.
  Release-docs validator not re-run (no release-doc change in this pass).
  `cargo deny` / feature tree / locked release build not re-run (no Cargo
  dependency/feature change). No test was claimed without running it.

## 5. Invariant review

Same invariants as M008 §4, unchanged by a one-line field copy: single
on-prem instance scope, TOML topology ownership, generation leases, single
SQLite gate/worker WAL/NORMAL, canonical wire and transparent streaming,
published protocol behavior, redaction, fail-closed limits. Routing remains
deterministic and load-based; the restoration strictly widens eligibility
back to the pre-M008 set (native behavior was already correct, transcode
regains its configured protocols).

## 6. Failure and recovery review

No new runtime path: the restored copy is a synchronous assignment inside
admission fact construction with no transaction, retry, selector, or
contention interaction. Budget denial/settlement paths are untouched;
re-verified green by the unchanged 19 `on_prem_budgets` + 13
`on_prem_global_budgets` tests inside the full suite.

## 7. Migration and compatibility review

No schema, config, API, CLI, wire, or database change. Zero migration
impact; all prior databases, rules, holds, and clients behave as before,
with transcode eligibility restored.

## 8. Security review

No security surface changed: no auth, secret, redaction, privilege, audit,
or tariff logic touched. The restored field is a routing-protocol list, not
identity or cost metadata.

## 9. Documentation and operations

This plan and closure record. A process note is recorded here rather than in
user docs: for runtime behavior changes, the before-push matrix in
`AGENTS.md` (which lists `uv run pytest tests/tooling/` unconditionally)
governs over a plan §11 reading of "tooling commands only when scripts
change", because the tooling suite drives the Rust binary. Future
runtime corrective plans should state the tooling-suite requirement
explicitly.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| Low | No Rust test pins the full transcode-eligibility matrix end to end; the guard pins the admission bridge, and Q007 pins the behavior in tooling/hosted CI. | A future bridge-adjacent edit is caught by the guard; deeper routing regressions rely on the tooling suite as before. | None — matches the pre-existing qualification architecture. |

No critical, high, or medium findings.

## 11. Roadmap disposition

M013 is closed. Unblock audit (same commit): M013 had no downstream hard
dependency — no registered plan declares it as a dependency — so nothing is
promoted. M010 and M011 stay ready (M010 unblocked by M008+M009; M011
independent and parallel). M012 stays blocked on M008–M011 closures plus
operational hardware. The M008/M009 closures stay immutable; this record is
the corrective account.

## 12. Registry updates

Applied in the same commit: M013 plan `active` → `implemented` with this
closure recorded as `closed`; roadmap M013 `active` → `closed`; M013 row
moved from active to recently-closed. All other rows unchanged. Historical
records remain immutable.
