# Plan 179 — Current-State Truth and Canonical Config Assets

Date: 2026-09-12
Status: ready for handoff
Parent roadmap: `plans/178-rust-maintenance-consolidation-roadmap.md`
Planning baseline: `78a4da64de3c94a9f5fe29e05e6bdf40402bc16b`
Priority: P1 maintenance / documentation authority / configuration packaging
Execution target: GPT-5.6 Luna/Sol or comparable implementation model

## Purpose

Make every active source, contributor guide, architecture guide, and bundled configuration example describe the application EggPool actually is today: a native Rust proxy with bounded Python repository/release tooling.

At the same time, remove the manually duplicated authority for the bundled configuration examples. This phase is deliberately behavior-light and should land before structural refactors so later work starts from truthful documentation and one canonical config example.

This is not a rewrite of historical records. Migration plans, `docs/migration-history.md`, changelog history, frozen release fixtures, and stable phase-coded regression names may retain historical language where it is accurate.

## Current-state findings

### Active Rust source still describes the retired migration state

At the planning baseline:

- `rust/src/lib.rs` says this is a side-by-side Rust implementation and Python remains production authority;
- `rust/src/config.rs` describes a side-by-side candidate and says Python is authoritative;
- `rust/src/server.rs` describes a migration candidate and phase-era C009/M7 boundaries;
- `rust/src/runtime.rs` emits `Rust migration candidate initialized`;
- additional module comments still use M/C/O migration milestone identifiers where a durable ownership description would be clearer.

Historical identifiers inside tests and frozen compatibility evidence are not automatically stale. The target is active explanatory text that tells a maintainer how the current system works.

### Current contributor/architecture navigation has Python-era runtime paths

`AGENTS.md` and active architecture deep dives still reference retired `src/eggpool/*.py` application modules and describe Python dispatch/runtime authority. At minimum audit:

- `AGENTS.md`;
- `architecture/README.md`;
- `architecture/overview.md`;
- `architecture/deep-dive-core.md`;
- `architecture/deep-dive-runtime.md`;
- other `architecture/**` pages reached from those documents;
- `.opencode/skills/**` development/architecture guidance;
- current operator docs under `docs/**`.

The `.opencode/skills/development/SKILL.md` current-runtime section is already Rust-oriented and should be treated as a useful reference rather than rewritten unnecessarily.

### `server.threads` documentation is operationally false

The canonical config example currently describes Granian runtime threads and asyncio. The actual executable uses:

```rust
#[tokio::main(flavor = "current_thread")]
```

`ServerConfig` still exposes `threads`, defaulting to `1`, and reload policy treats that public field as restart-required. Do not silently reinterpret this compatibility key as Tokio worker count during a documentation cleanup.

### Config examples are duplicated committed authorities

The following pairs are currently byte-identical committed files:

```text
config.example.toml
rust/assets/config/config.example.toml

config.sbc.example.toml
rust/assets/config/config.sbc.example.toml
```

`operations/config_mutation.rs` embeds the Rust-assets copy with `include_str!`. `rust/build.rs` already has a deterministic generation pattern using repository inputs and `OUT_DIR` for the migration inventory.

## Governing constraints

1. Do not change request routing, provider behavior, database behavior, protocol translation, update/rollback, or operator command semantics in this phase.
2. Preserve public configuration parsing compatibility. Do not delete `server.threads` here.
3. Preserve Tokio `current_thread` execution unless a separate performance/runtime plan deliberately changes it.
4. Historical documentation may use Python/migration/cutover terminology when explicitly describing history.
5. Do not mass-rename phase-coded tests or internal stable identifiers merely for aesthetics.
6. Do not create a documentation-generation framework.
7. Prefer one canonical committed config source. If package/build constraints make direct use of the repository-root file unsafe, retain a generated build copy or enforce exact equality mechanically; do not return to two hand-edited authorities.
8. Keep active docs concise enough that architecture docs, not completed implementation plans, are the primary description of the current system.

## Workstream A — Build a scoped current-state truth inventory

Search active surfaces for stale authority language. Representative searches:

```bash
rg -n "side-by-side|migration candidate|Python remains|Python implementation|Granian|asyncio|src/eggpool/|runtime_dispatch\.py|request_coordinator\.py" \
  rust/src AGENTS.md architecture docs .opencode/skills config.example.toml config.sbc.example.toml
```

Classify each hit as:

- inaccurate current-state explanation — fix;
- useful explicit historical context — retain and label clearly;
- stable test/compatibility identifier — retain unless confusing current behavior;
- dead current documentation — remove rather than translating obsolete detail line-by-line.

Do not use a global search-and-replace over `plans/`, CHANGELOG, migration history, fixtures, or test names.

## Workstream B — Rewrite crate/module authority comments

Update active top-level/module documentation so it names current ownership rather than migration phases.

At minimum:

- `rust/src/lib.rs`: describe the native EggPool crate/application boundary and Python tooling boundary;
- `config.rs`: describe typed TOML schema/load/validation ownership;
- `server.rs`: describe Axum HTTP/server adapter ownership and coordinator handoff;
- `runtime.rs`: describe CLI dispatch/operator bootstrap ownership and remove migration-candidate diagnostic text;
- touched `operations/*`, lifecycle, coordinator, and wire comments only where the old milestone wording obscures durable ownership.

Do not erase useful semantic invariants just because their comments originated during migration. Rephrase them in terms of current modules and contracts.

## Workstream C — Repair contributor and architecture authority

Update `AGENTS.md` so current-runtime pointers reference the actual Rust modules/tests/scripts. Remove representative Python application file lists that no longer exist.

For architecture pages, prefer a compact current graph such as:

```text
CLI/runtime adapter -> operations services
server/Axum -> coordinator -> routing/provider transport
                         -> wire canonical codecs/stream
runtime manager -> generation factory -> supervised background tasks
config parser -> transition policy -> transactional reload/publication
SQLite repositories <- accounting/catalog/health/maintenance
```

Document Python only as release/validation tooling and historical package compatibility where applicable.

Delete obsolete architectural prose rather than preserving a Python description next to an equivalent Rust description.

## Workstream D — Clarify `server.threads`

Trace every live reader/test of `Config.server.threads` before changing wording.

For this phase, preserve the key and its accepted values unless an existing validation rule is already stricter. Document the truth explicitly:

- EggPool currently runs a Tokio current-thread runtime;
- `server.threads` is retained for configuration compatibility/current diagnostics and does not select a Granian/asyncio or Tokio worker pool;
- changing it remains classified according to the existing restart policy;
- a future removal or real multithread implementation requires a separate compatibility/performance decision.

If values other than `1` are currently accepted but ignored, retain that behavior in this plan and make the warning/diagnostic explicit rather than turning this maintenance pass into a config-breaking change.

## Workstream E — Establish one config-example authority

Make the repository-root examples the preferred canonical human-edited source because README/docs already link to them.

Preferred implementation:

1. teach `rust/build.rs` (or a smaller build-support helper) to read the repository-root `config.example.toml` / `config.sbc.example.toml` as Cargo-tracked inputs;
2. copy or generate deterministic build outputs under `OUT_DIR` when an embedded asset is required;
3. make `operations/config_mutation.rs` embed the generated/canonical build artifact;
4. remove `rust/assets/config/` committed duplicates if all supported build/package contexts still work.

A direct relative `include_str!` of the repository-root file is acceptable only if source, release, Maturin/PyPI, and standalone build contexts all reliably include that path. Verify before choosing it.

If packaging constraints require retaining committed Rust-local copies, do **not** leave them manually synchronized: add a deterministic equality validator/test with a clear canonical direction and fail on drift.

Do not add templating for two files that are already valid TOML.

## Workstream F — Add bounded regression coverage

Add or extend tests only where they protect a durable invariant:

- `init-config` emits/parses the canonical default configuration;
- the SBC/default config sources stay canonical if a copy remains;
- crate/package build still embeds the expected default configuration;
- current `server.threads` diagnostics/documented runtime model match code.

A repository-wide terminology denylist in every CI run is unnecessary. If a lightweight validator is added, scope it to explicit current-authority files and allow historical contexts rather than banning words globally.

## Required verification

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test build_manifest -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o004 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked
cargo build --manifest-path rust/Cargo.toml --locked --release
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
git diff --check
```

If the config asset location/build path changes, also execute the current Maturin/package-boundary smoke used by release tooling so source/wheel builds prove the canonical example is present.

Repeat the scoped current-state search and manually inspect all remaining hits.

## Acceptance criteria

- Active Rust crate/module documentation no longer claims Python is production authority or calls the current binary a migration candidate.
- `AGENTS.md` and active architecture navigation point to current Rust implementation authority rather than retired `src/eggpool/*.py` modules.
- Granian/asyncio wording is removed from current Rust configuration guidance.
- `server.threads` truthfully describes its current compatibility/runtime semantics without an unplanned public config break.
- Default and SBC config examples have one human-edited authority or an enforced one-way synchronization contract.
- `eggpool init-config` continues to produce a valid current configuration.
- Supported source, standalone, and PyPI/Maturin build contexts remain green.
- Historical evidence is not mass-edited or destroyed.

## Handoff note

Treat this as authority repair, not terminology beautification. The important result is that a maintainer can read the current source/docs and identify the real Rust owner of each behavior, while historical material remains accurate history.