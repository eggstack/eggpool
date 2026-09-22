---
name: development
description: Development, formatting, linting, type checking, and testing for the Rust runtime and Python tooling.
---

# Development Workflow

## Current runtime

Run from the repository root:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked
cargo build --manifest-path rust/Cargo.toml --locked --release

# Shared reusable crates (Rust 1.81-compatible boundaries)
cargo check --manifest-path rust/crates/eggpool-model-routing/Cargo.toml
cargo test --manifest-path rust/crates/eggpool-model-routing/Cargo.toml
cargo test --manifest-path rust/crates/eggpool-client-config/Cargo.toml
```

Strict Clippy is a repository invariant across all Rust targets. Do not add a
baseline allowlist or broad suppression; resolve new warnings locally and use
narrow, justified allowances only when the intentional API or test shape is
clearer and safer.

For adapter changes, run the CLI contract, operations O002–O010,
`status_command`, health, and coordinator publication/boundary targets before
the full workspace suite. Keep the server modules thin: HTTP handlers must
delegate inference lifecycle work to the coordinator (health/status handlers
only project authoritative state), and lifecycle workflows must compose the
existing process safety primitives.

Native test targets live in `rust/tests/` (serial `--test-threads=1`). The
coordinator suite is `coordinator_c007`–`c011`, `c013`–`c014` (there is no
`c012`) plus `coordinator_boundaries`/`finalization`/`publication`; routing is
`routing_domain`, `routing_domain_d008`, `routing_claims`, `quota`; lifecycle is
`runtime_lifecycle_r002`–`r013`; wire is `wire_codecs`, `wire_stream`,
`wire_runtime`, `wire_qualification`, `wire_adaptation`, `wire_profiles`,
`wire_multimodal`; operations is `operations_o002`–`o010` plus
`status_command` (Plan 202 provider/proxy health aggregation, `/api/status`,
CLI offline behavior).

For `configsetup`/`configremote`/integration-profile/`eggpool-connect`
changes, run the portable crate and helper plus the focused O005 contract
target alongside the integration unit tests:

```bash
cargo test --manifest-path rust/crates/eggpool-client-config/Cargo.toml
cargo test --manifest-path rust/Cargo.toml -p eggpool-connect -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o005 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib operations::integrations
cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r005 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test status_command -- --test-threads=1
```

For helper-only work, qualify the narrow dependency surface separately:

```bash
cargo tree --manifest-path rust/Cargo.toml -p eggpool-connect -e features
cargo build --manifest-path rust/Cargo.toml -p eggpool-connect --locked
```

For streaming coordinator changes, run the focused C008 publication, boundary,
finalization, and wire suites before the workspace suite (add `coordinator_c009`
and `coordinator_c011` for terminal/retry behavior changes):

```bash
cargo test --manifest-path rust/Cargo.toml --test coordinator_c008 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_finalization -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_publication -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_stream -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_qualification -- --test-threads=1
```

For request hot-path or provider ownership changes, also qualify the single
endpoint boundary and immutable client topology before the full suite:

```bash
cargo test --manifest-path rust/Cargo.toml --test coordinator_c009 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_c011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test coordinator_boundaries -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
```

Plans 230–234 require comparable release/loopback evidence for parse and
body-copy work. Treat dashboard SQLite, the streaming mpsc bridge, the
current-thread runtime, and the routing selection lock as measurement targets;
do not add a second database connection, runtime worker pool, broad queue, or
lock-free routing structure without reproducible tail-latency evidence.

For cancellation and ownership tests, synchronize on an observable fixture
transition or product invariant under a bounded `tokio::time::timeout`.
Avoid fixed millisecond sleeps and fixed `yield_now()` counts as readiness or
cleanup proofs. Provider cancellation fixtures should expose accepted-TCP or
equivalent gates, keep accepted and completed-handshake counters separate, and
assert client recovery plus no cancelled request reaching the origin. When no
narrow external-fixture boundary exists, use the smallest cooperative
criterion available and document the limitation; do not add arbitrary timeout
inflation or production ownership changes. Repeat originally flaky tests at
least 100 times before closing the cleanup.

For Responses admission or wire-preservation changes, also run the focused
canonical request, wire qualification, and coordinator stateless-contract
targets. Verify native same-surface alias rewriting and cross-surface rejection
before the workspace suite. For Responses streaming changes, also verify native
unknown-event preservation, item-id/call-id mapping, authoritative
`response.output_item.done` synthesis, bounded encoder overflow, and strict
`response.completed`/EOF behavior in `wire_stream` and `wire_runtime`.
Native observer changes must additionally compare the internal fold summary with
the public collecting decoder on split frames, unknown events, usage, terminal
failure, malformed input, and EOF; do not add a second SSE parser.
Codex compatibility changes additionally run the deterministic
`codex_responses_compat` target. It covers native request preservation,
function/freeform/deferred-search wrapper round trips, interleaved
parallel-call identity, declaration-scoped `tool_search` reconstruction,
ordinary-`tool_search`-name non-reclassification, hosted-search
pre-dispatch rejection, malformed-wrapper rejection, and the current Codex
`output_item.done`/terminal contract; no Codex runtime dependency or
credential is required. Remote-compaction
changes additionally run the deterministic `codex_compaction_compat` target. It
covers compact admission, native alias rewriting, byte-exact native forwarding,
bounded compact success/failure validation, explicit unsupported-target rejection,
stateless/finite bounds, diagnostic redaction, and v2 trigger rejection-or-
preservation by capability. The opt-in
`scripts/smoke_codex_compat.sh` separately qualifies a current CLI with both a
fixed text request and a random-marker read-only shell-tool loop, returning 77
when live credentials are unavailable.

Keep post-handoff execution single-owner and incremental while refactoring;
transparent upstream replay is only valid before `StreamingExecution` is
returned.

For compact production ownership changes, run `codex_compaction_compat`, the
finite coordinator C008/C009/C011 plus boundary/finalization/publication
suites, and verify that public `FiniteRequest` compatibility constructors are
unchanged. The production endpoint may use a private single-owner compact
input to avoid duplicating the preserved JSON tree.

Plan 234 release qualification is evidence-gated: confirm Maturin 1.14.1's
explicit `--strip false` semantics, run the locked release build and artifact
validators, and treat ThinLTO/stripping as ephemeral experiments unless every
published target qualifies.

Configuration changes must use `config_reload_policy::classify_transition`.
Mutation paths should carry the redacted transition into apply logic, while
`reload.rs` remains authoritative for server-side revalidation and generation
publication. Add deterministic transition coverage for no-op, live,
restart-required, mixed, invalid, and secret-redaction cases.

The optional physical-SBC target-class pass reuses
`scripts/qualification_sbc.py`; it is not a CI or Rust-runtime benchmark:

```bash
uv run pytest tests/tooling/test_qualification_sbc.py -q
uv run python scripts/qualification_sbc.py \
  --binary rust/target/release/eggpool \
  --config-fixture tests/tooling/fixtures/qualification/sbc-benchmark.toml \
  --benchmark-samples 30 \
  --output artifacts/qualification/236-sbc-benchmark-run-1.json
```

Run ordinary qualification first without benchmark mode (`runtime-q008.v1`),
then run the benchmark only on a physically attested Linux/aarch64 SBC, three
times from fresh roots with the benchmark-only low-wear fixture
(`runtime-q008.v2`). Keep its sanitized aggregate output in the plan evidence;
never substitute a hosted ARM VM or add a CI hardware job.

The diagnostic-only `--diagnose-finite-tail 60` flag (10–200, default off,
requires benchmark mode) appends sequential native-finite phase timing plus a
direct-provider control for tail localization; it is scalar-only with no p99.
On Pi 5 it localized the finite tail to the pre-provider durable publication /
SQLite / storage path (stable direct control; one tmpfs run removed the
tail), so only a narrow database/publication follow-up is justified — never a
Tokio, routing-lock, or streaming change from this evidence.

Plan 238's `--diagnose-publication-storage 20..=200` mode is separate from
the Plan 236 benchmark corpus and uses the benchmark fixture directly. It
waits for checkpoint/metrics/task quiescence, records only fixed-name task
tick deltas and one bounded WAL-header read per request, and can place only
the SQLite database/WAL/SHM in a caller-selected temporary filesystem. It is
physical-SBC evidence only, never a CI benchmark or runtime optimization.

Plan 239 adds a separate feature-enabled qualification build:

```bash
cargo build --manifest-path rust/Cargo.toml --locked --release \
  --features qualification-db-diagnostics
uv run python scripts/qualification_sbc.py \
  --binary rust/target/release/eggpool \
  --config-fixture tests/tooling/fixtures/qualification/sbc-benchmark.toml \
  --diagnose-publication-phases \
  --output artifacts/qualification/239-h0-run-1.json
```

The mode requires a physical Linux/aarch64 SBC, captures exactly 60 sequential
native finite requests, and rejects missing/duplicate foreground records,
background task ticks, and non-convergence. H1 adds
`--qualification-wal-autocheckpoint-pages 0`; H2 at `256` is conditional on
the plan predicate. The collector and runtime field are absent from ordinary
builds; do not treat this mode as CI or a production SQLite recommendation.

For native dependency or feature changes, Cargo is the authority. Review both
the source/build/test owners and the resolved graph before removing a direct
crate or feature:

```bash
cargo deny --manifest-path rust/Cargo.toml check
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
```

The provider transport currently requires exact `eggfetch-core =0.2.0` with
`native-http1,tls-rustls` (not the high-level `http1` alias or
`standard-http1`). Dependency/profile changes must run both provider transport
targets and the C008/C009/C011 plus boundary/finalization/publication suites
before the full workspace checks.

The Eggress SSH capability is intentionally optional. Feature changes affecting
provider transport must also qualify the reduced surface:

```bash
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --no-default-features
```

No-default builds must preserve direct and non-SSH proxy construction while
returning `TransportError::ProxyConfiguration` for SSH proxy configuration.
Default SSH uses the root `ssh` capability forwarded to Eggress 1.0.8
(`eggress-outbound/ssh` plus the compatibility crate's SSH translation
support; pproxy-style SSH needs both); no Eggpool-owned
SSH executor fallback is permitted. The production dial path uses
`connect_tcp_detailed` with a typed kind/stage adapter; no message-string
error classifier is permitted.

`cargo deny` checks RustSec advisories, the reviewed license allowlist, allowed
registry/git sources, and duplicate-version warnings from `deny.toml`. It does
not replace strict Clippy/tests or owner-specific qualification. When
`rust/Cargo.toml` or `rust/Cargo.lock` changes, also run the locked release
build and serial workspace suite:

```bash
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
```

Keep supported Eggress proxy URI/chaining, TLS verification, and bundled
SQLite/backup behavior qualified when changing their feature sets. The
dependency audit workflow runs on dependency-policy changes, weekly, and by
manual dispatch; ordinary source-only CI does not wait on its network advisory
database.

## Tooling

Python is retained for release/validation scripts and their tests only:

```bash
uv sync --dev
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
```

Focused release checks include the catalog, package boundary, release
workflow, retirement boundary, and quick-installer qualification validators.
Do not add a Python application fallback or import the retired application.
