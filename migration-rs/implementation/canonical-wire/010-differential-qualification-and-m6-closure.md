# W010 — Differential Qualification and M6 Closure

Status: dependency-ready; W009 closure accepted

Source roadmap: `migration-rs/subsystems/canonical-wire-roadmap.md#w010--differential-qualification-and-m6-closure`

Primary class: invariant

Hard dependency: W009 accepted closure.

## 1. Objective

Qualify integrated M6 against the W001 Python oracle and prove M7 can safely depend on it. This is a closure pass, not a place for new surfaces, IR redesign, or coordinator/provider dispatch.

## 2. Integrated matrix

Exercise client bytes -> admission -> canonical request -> explicit upstream profile -> encoded body; finite upstream -> canonical/provider evidence -> client body; stream bytes -> SSE/profile decode -> canonical/client events; usage/terminal evidence; adaptation warnings/rejections; and pure M5 routing/affinity bridges. Cover all four supported families and semantically distinct built-in profiles.

## 3. Differential rules

Only approved incidental normalization is allowed. Semantic content/tool/reasoning/media presence, model/profile identity, warning/rejection category, finish/terminal category, usage/cache zero-vs-missing, limit accept/reject, tool IDs/order, structured-output requirement, and provider-error-vs-malformed classification are exact/semantic mandatory fields.

## 4. Cross-wire and adversarial coverage

Use feature-rich representative fixtures across every supported source/target family pair: roles, reasoning, tools, structured output, images/documents, cache controls, finite usage, and streaming deltas. Also cover malformed/oversized client JSON, bounded collection edges, invalid schemas/tools/media, malformed provider payloads/SSE, one-byte fragmentation, EOF lifecycle phases, unknown profiles, and unsupported adaptations under configured policy modes.

No case may panic, leak unrelated state, or require restart recovery.

## 5. Resource/dependency review

Record representative normal request copies/allocations, bounded media/document behavior, SSE carry state under fragmentation, absence of per-event tasks, and profile registry size. Do not invent hard performance gates; flag obvious pathology such as repeated full-body parsing or whole-stream buffering.

Review every M6 Cargo dependency addition. Reject accidental second HTTP clients, actor frameworks, heavy schema/reflection or media/OCR stacks, or generic streaming frameworks without demonstrated need.

## 6. Boundary audit

Prove M6 contains no provider network send, auth-secret injection, request/attempt/wire-preference SQLite writes, account selection/claim mutation, dynamic wire learning/retry, health effects, downstream response handoff, timeout/cancellation policy, finalization, or semantic model-router selector invocation.

## 7. M5/security regression

Run accepted M5/D009 tests and prove the W002 fact bridge is pure. Use synthetic sentinels to ensure request/media/schema content, sessions, credentials/proxy data, and full provider bodies do not leak through default debug/errors/tracing metadata.

## 8. Required verification

At minimum record results for:

```text
cargo fmt --manifest-path rust/Cargo.toml -- --check
cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
uv run pytest <targeted request/wire/transcoder/sse/usage Python suites> -q --tb=short --maxfail=1
uv run ruff format --check src/ tests/ scripts/
uv run ruff check src/ tests/ scripts/
uv run pyright src/ scripts/
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
git diff --check
```

No paid/live inference is required. If an unrelated all-target blocker exists, document it and run every affected M6/M5 target independently; new unit tests alone are insufficient.

## 9. Closure record

Create `migration-rs/closure/canonical-wire/010-status.md` with baseline/head commits, W001-W010 requirement-to-evidence matrix, fixture/profile counts, cross-wire/chunking evidence, resource/dependency/security audit, exact verification, unresolved findings, supported differences/ADRs, and registry/roadmap transition.

## 10. Acceptance criteria

M6 closes only if all mandatory W001 observations match semantically; all four families/profile distinctions are covered; finite/stream transformations preserve material intent or explicitly warn/reject; usage/cache and terminal evidence match; adversarial input remains bounded; M5 remains green; dependency posture fits local/SBC deployment; no M7 behavior is embedded; and no unresolved high/medium correctness/security finding remains.

After W010 closure, mark M6 closed and allow M7 planning/implementation handoff to become dependency-ready only through its own planning review.

## 11. Stop conditions

Do not close if a supported surface lacks integrated coverage, EOF/error can become false stream success, material semantics are silently lost, M6 owns retry/network/finalization state, resources are unbounded, diagnostics can leak secrets/raw bodies, or closure relies on live-provider success rather than deterministic parity evidence.
