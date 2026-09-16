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

For `configsetup` integration changes, run the portable crate plus the
focused O005 contract target alongside the integration unit tests:

```bash
cargo test --manifest-path rust/crates/eggpool-client-config/Cargo.toml
cargo test --manifest-path rust/Cargo.toml --test operations_o005 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib operations::integrations
cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1
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

For Responses admission or wire-preservation changes, also run the focused
canonical request, wire qualification, and coordinator stateless-contract
targets. Verify native same-surface alias rewriting and cross-surface rejection
before the workspace suite. For Responses streaming changes, also verify native
unknown-event preservation, item-id/call-id mapping, authoritative
`response.output_item.done` synthesis, bounded encoder overflow, and strict
`response.completed`/EOF behavior in `wire_stream` and `wire_runtime`.
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

Configuration changes must use `config_reload_policy::classify_transition`.
Mutation paths should carry the redacted transition into apply logic, while
`reload.rs` remains authoritative for server-side revalidation and generation
publication. Add deterministic transition coverage for no-op, live,
restart-required, mixed, invalid, and secret-redaction cases.

For native dependency or feature changes, Cargo is the authority. Review both
the source/build/test owners and the resolved graph before removing a direct
crate or feature:

```bash
cargo deny --manifest-path rust/Cargo.toml check
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
```

The Eggress SSH compatibility fallback is intentionally optional. Feature
changes affecting provider transport must also qualify the reduced surface:

```bash
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --no-default-features
```

No-default builds must preserve direct and non-SSH proxy construction while
returning `TransportError::ProxyConfiguration` for SSH proxy configuration.

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
