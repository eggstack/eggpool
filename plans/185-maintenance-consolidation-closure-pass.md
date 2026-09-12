# Plan 185 — Maintenance Consolidation Closure Pass

Date: 2026-09-12
Status: ready for handoff
Parent roadmap: `plans/178-rust-maintenance-consolidation-roadmap.md`
Planning baseline: `78a4da64de3c94a9f5fe29e05e6bdf40402bc16b`
Priority: P1 closure / regression qualification / ownership audit
Execution target: GPT-5.6 Luna/Sol or comparable implementation model

## Purpose

Close Plans 178–184 only after proving that the maintenance work actually reduced authority overlap and stale current-state documentation without changing EggPool's externally visible behavior or introducing a new maintenance framework.

This is a verification/closure pass, not a final opportunity to add unrelated cleanup. Any newly discovered semantic bug should receive a narrow corrective change/plan with its own tests rather than being hidden in closure refactoring.

## Preconditions

Begin this plan only after the intended implementation of Plans 179–184 has landed or each intentionally deferred item has a documented reason.

Expected completed areas:

- active source/docs describe the native Rust runtime truthfully;
- canonical config examples have one authority or mechanically enforced synchronization;
- CLI/runtime operational mechanisms are owned under `operations` and `runtime` is primarily an adapter;
- server HTTP/control-plane concerns are split by durable surface while coordinator ownership is preserved;
- config mutation and live rehash consume one typed transition policy;
- runtime generation/task lifecycle code is decomposed without state-machine changes;
- streaming coordinator is decomposed internally while wire/transport boundaries remain intact;
- dependency advisory/license/source policy is active with bounded automation.

## Governing constraints

1. Do not use closure as justification for feature expansion.
2. Do not impose line-count/file-count thresholds. Review whether ownership is discoverable and singular.
3. Do not delete historical plans/tests solely because their identifiers mention migration phases.
4. Do not weaken tests, lint policy, dependency policy, or safety checks to obtain a green closure run.
5. Do not add another registry/closure framework. This completed plan and normal Git history are sufficient evidence.
6. Preserve local/LAN/SBC scope; do not add production-gateway infrastructure during closure.
7. If a release/package/update contract was not touched by the implementation, do not require a live/public release merely to close maintenance work.

## Workstream A — Re-audit current ownership

Inspect the final module tree and answer explicitly:

- Does `runtime` still implement reusable process/deploy/update/config mechanisms that belong to `operations`?
- Does `server` contain routing/retry/wire/finalization policy rather than HTTP adaptation?
- Does `reload` own publication while config transition policy remains pure/typed?
- Does any config mutation path maintain a second restart/reload key list?
- Does `runtime_lifecycle` preserve one active-generation manager and explicit lease/retirement ownership?
- Does `task_supervisor` remain the sole owner of background task handles?
- Does coordinator streaming own lifecycle rather than raw protocol parsing?
- Does `wire::WireStream` remain the canonical incremental stream/terminal-evidence owner?
- Does provider transport remain neutral to provider credentials/routing/finalization?

If the answer to any boundary question is no, correct the smallest ownership leak before closure.

## Workstream B — Re-audit active current-state truth

Run a scoped scan over current authority surfaces, excluding explicit history/plans/fixtures as appropriate:

```bash
rg -n "side-by-side|migration candidate|Python remains|Python implementation|Granian|asyncio|src/eggpool/|runtime_dispatch\.py|request_coordinator\.py" \
  rust/src AGENTS.md architecture docs .opencode/skills config.example.toml config.sbc.example.toml
```

Manually classify remaining hits. Historical comparison wording is acceptable only where the page clearly says it is history/compatibility context.

Confirm:

- `rust/src/lib.rs` describes the current Rust application;
- architecture/AGENTS point to current Rust owners;
- Python is described as repository/release tooling and historical-package compatibility only;
- `server.threads` current-thread/compatibility behavior is truthful;
- no current operator doc instructs users using retired Python runtime commands/modules.

## Workstream C — Verify config asset authority

Prove the default and SBC config examples cannot drift silently.

If the Rust runtime now embeds repository-root canonical sources/generated `OUT_DIR` copies:

- confirm no hand-edited duplicate remains;
- confirm Cargo rebuild tracking includes the canonical inputs;
- confirm source/release/Maturin builds can see those inputs.

If committed package-local copies were necessarily retained:

- run the deterministic equality/canonical-source validator;
- confirm documentation clearly identifies which copy is authoritative.

Run `eggpool init-config` in a disposable location and parse/check the resulting file through the native config command.

## Workstream D — Verify configuration transition singularity

Inspect code—not only tests—to confirm:

- one field-disposition table/classifier exists;
- restart-required paths are not duplicated in runtime/config-mutation/reload;
- mixed restart/live changes do not partially publish;
- client-side mutation classification is not trusted instead of server-side revalidation;
- digest mismatch and candidate validation still fail before publication;
- generation build failure leaves current generation authoritative;
- no credential values enter config diff/debug/operator output.

Run focused operations and R011–R013 tests before the full suite.

## Workstream E — Verify lifecycle/streaming invariants after decomposition

For runtime lifecycle, specifically re-check:

- startup crash reconciliation before admission;
- immutable generation construction before publish;
- `ArcSwap` active authority;
- request leases across awaits;
- bounded retirement and terminal-reference/finalization drain;
- provider/client close ordering;
- process-vs-generation task ownership;
- graceful/forced shutdown diagnostics.

For streaming, specifically re-check:

- header and first-byte retry window only before handoff;
- no replay after downstream start;
- incremental bounded wire decoding;
- idle timeout but no whole-stream lifetime timeout;
- non-SSE compatibility path;
- terminal-evidence classification;
- client cancellation/drop durable finalization;
- exactly-once usage/accounting completion.

Do not accept structural readability as a substitute for these behavioral checks.

## Workstream F — Verify dependency policy quality

Run the final dependency policy and inspect warnings/exceptions:

```bash
cargo deny --manifest-path rust/Cargo.toml check
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
```

Confirm:

- no actionable advisory is ignored without a documented blocking reason;
- source policy rejects unexpected registries/git dependencies;
- license allowlist/exceptions reflect the actual graph;
- duplicate-version policy does not generate meaningless blockers;
- dependency-audit workflow is path/schedule/manual bounded as intended;
- a successful run has occurred with the committed policy.

Do not reopen Plan 170 feature minimization unless the dependency audit itself finds a real security/compatibility reason for a separate upgrade/removal.

## Workstream G — Informational maintenance measurements

Record final source/module sizes and direct/lock package counts only as diagnostics. Compare with the planning baseline to answer whether the work actually separated responsibilities.

Do **not** establish file-size, binary-size, package-count, or test-count thresholds from these measurements.

Useful checks may include:

```bash
wc -c rust/src/runtime.rs rust/src/server.rs rust/src/runtime_lifecycle.rs rust/src/coordinator/streaming.rs 2>/dev/null || true
cargo tree --manifest-path rust/Cargo.toml --edges normal --prefix none | sort -u | wc -l
```

If files became directories, summarize their module ownership rather than manufacturing a combined-size target.

## Full closure verification

Run the full current repository baseline:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo check --manifest-path rust/crates/eggpool-model-routing/Cargo.toml
cargo test --manifest-path rust/crates/eggpool-model-routing/Cargo.toml
cargo deny --manifest-path rust/Cargo.toml check
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
```

Also run the focused suites from Plans 180–183. Where implementation touched package/release inputs, run the corresponding existing package-boundary/release validators and a local Maturin/package smoke. Do not perform a public release as a closure prerequisite.

## Deferred feature audit

Before declaring the roadmap complete, confirm the following were intentionally **not** smuggled into maintenance work:

- stateful OpenAI Responses/conversation persistence;
- embeddings/images/audio endpoint families;
- Prometheus/OpenTelemetry export;
- persistent semantic model-routing affinity;
- HA/distributed coordination;
- RBAC/multi-tenancy/inbound production gateway features;
- more aggressive Eggress compatibility removal;
- multithread Tokio runtime behavior.

These remain separate product/performance decisions and may be planned later from evidence.

## Acceptance criteria

- all Plans 179–184 acceptance criteria are satisfied or an explicit narrow deferral is recorded;
- active docs/source are current-state truthful;
- config examples have a single enforceable authority;
- CLI/server/config/lifecycle/streaming ownership is clearer with no duplicated semantic authority;
- all critical lifecycle/streaming/reload contracts remain behaviorally qualified;
- dependency advisory/license/source policy is green and operational;
- strict Clippy and the complete Rust/tooling suites pass;
- package/release validation remains green where touched;
- no maintenance-only framework or product feature was added;
- roadmap 178 can be marked complete with concise commit/test evidence.

## Handoff note

Closure should be evidence-driven. If the code is merely split into more files but authority is still duplicated, the roadmap is not complete. Conversely, do not continue refactoring already-cohesive modules simply because additional large files remain elsewhere in the repository.