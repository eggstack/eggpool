# Plan 169 — Rust Clippy Baseline Elimination and CI Gate

Date: 2026-09-11
Status: complete (verified 2026-09-11)
Parent roadmap: `plans/168-rust-production-cleanup-roadmap.md`
Priority: P1 correctness / maintenance
Execution target: GPT-5.6 Luna/Sol or comparable implementation model

## Purpose

Remove the migration-era strict-Clippy exception now that Rust is the sole production runtime, and make static Rust quality an ordinary repository invariant.

At plan opening, closure evidence recorded 66 Clippy errors and one warning
under `--all-targets -- -D warnings`. Ordinary CI did not run Clippy, so new
findings could accumulate without being distinguished from the historical
baseline.

This plan makes Clippy green without broad suppression, semantic redesign, or test weakening, then adds the exact strict gate to CI and contributor guidance.

## Governing constraints

1. Preserve runtime behavior unless a finding exposes a genuine correctness defect.
2. Do not use crate/module-wide `allow(clippy::all)`, `allow(warnings)`, or a committed baseline allowlist.
3. A narrow `#[allow(clippy::<lint>)]` is acceptable only when the alternative worsens correctness/readability or conflicts with an intentional API shape; include a concise local rationale.
4. Do not change public config/wire/database semantics merely to satisfy style lints.
5. Do not delete meaningful tests to reduce findings.
6. Review clone/ownership changes around async code for lifetime, cancellation, and concurrency semantics before applying them mechanically.
7. Treat panic/unwrap-like, lossy-conversion, suspicious arithmetic, error-discarding, async-lock, indexing, and ownership findings as higher priority than stylistic findings.
8. Do not add another linter/service.
9. Add the CI gate only after the repository itself passes it.

## Workstream A — Capture and classify the baseline

Run exactly:

```bash
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
```

Create only a temporary working classification by lint and file. Classify findings as correctness/error handling, async/concurrency/ownership, numeric/conversion/bounds, API/readability, test-only fixture noise, or intentional false positive.

Prioritize findings in provider transport, retry/finalization, runtime-generation lifecycle, SQLite transaction ownership, updater recovery, TLS/auth, and redaction; those require focused regression verification after correction.

## Workstream B — Correct production findings first

Prefer idiomatic local transformations that preserve control flow and error identity.

Required review rules:

- preserve causal error chains and sanitized outward errors;
- preserve checked/saturating arithmetic and bounds;
- preserve bounded buffers, timeouts, and semaphore ownership;
- preserve `Send`/`Sync` and cancellation semantics;
- preserve exact request-attempt accounting and no retry after downstream handoff;
- preserve SQLite task ownership and ambiguity/rollback behavior;
- preserve credential/request-content redaction.

If a lint exposes a real defect, add a focused regression rather than treating it as style cleanup.

## Workstream C — Resolve test-only lint debt

Apply the same strict gate to all targets. Simplify fixtures/helpers rather than suppressing entire test modules. Preserve deterministic synchronization, bounded shutdown, exact account-isolation/provider-submission assertions, state persistence, updater rollback, and exact-version transition behavior.

Do not replace barriers/events with sleeps or retry loops. Use narrow justified lint allowances when a test fixture is otherwise clearer and semantically safer.

## Workstream D — Make Clippy an ordinary CI invariant

After a clean local run, update `.github/workflows/ci.yml` to run:

```bash
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
```

Keep the existing single CI job; place Clippy after formatting and before/adjacent to tests. Adjust timeout only if measured runtime requires a small bounded increase.

Update `AGENTS.md` so the local/before-push checks include strict Clippy and state that new warnings are not accepted as a baseline. Do not create a separate lint workflow.

## Required verification

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
```

Run focused Rust targets for any sensitive subsystem changed by the fixes.

## Acceptance criteria

- Strict Clippy passes with zero errors/warnings across all targets.
- No broad suppression or committed baseline allowlist exists.
- Narrow allowances are justified locally.
- Correctness-relevant findings have regression coverage.
- Main CI executes strict Clippy and remains one bounded job.
- `AGENTS.md` before-push guidance matches CI.
- No high/medium behavior regression is introduced.

## Handoff note

Do not combine this with broad architectural refactoring. The objective is to convert an accepted migration exception into a normal production invariant with the minimum safe code changes.

## Closure evidence

Implemented in `ec7e19968a07e0064a76a302db756a9acdd890ad`. Strict Clippy now
runs in `.github/workflows/ci.yml` with `-D warnings` across all targets, and
the local Rust/tooling verification suite passed on the completed cleanup
tree. Hosted CI subsequently passed for head
`c96ab4a512de1f622edf9a909ac8418a36570127` in run `34652706163` after the
release-workflow indentation correction.
