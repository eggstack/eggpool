# Plan 181 — Canonical Configuration Transition Authority

Date: 2026-09-12
Status: ready for handoff
Parent roadmap: `plans/178-rust-maintenance-consolidation-roadmap.md`
Planning baseline: `78a4da64de3c94a9f5fe29e05e6bdf40402bc16b`
Priority: P1 correctness / reload safety / maintenance
Execution target: GPT-5.6 Luna/Sol or comparable implementation model

## Purpose

Make every configuration-changing path consume one typed load/validate/classify contract so `set`, connect/logout/onboarding mutations, explicit `rehash`, and internal generation publication cannot develop conflicting ideas about what is valid, reloadable, restart-required, or unchanged.

The current modules already have the right broad responsibilities. This plan consolidates **authority**, not files.

## Current-state findings

The current config lifecycle is spread across four legitimate layers:

- `config.rs` owns the public TOML schema, defaults, loading, and semantic validation;
- `config_reload_policy.rs` owns field dispositions, reloadable/restart-required classification, `ReloadableConfig`, and `ReloadDiff`;
- `reload.rs` owns staging, generation construction, atomic publication, retirement, and rehash results;
- `operations/config_mutation.rs` owns bounded/atomic text edits and operator apply behavior.

The risk is not that these modules exist. The risk is parallel decision paths between them.

`config_mutation.rs` currently validates edited bytes through `Config`, then for live application sends a control request and lets the server discover the final reload disposition. For restart-oriented mutations it calls back into `crate::runtime::restart_for_mutation`, creating an avoidable operations-to-runtime dependency. Plan 180 should move that lifecycle mechanism under operations; this plan then makes config transition classification explicit and reusable.

`config_reload_policy.rs` already contains most of the durable policy needed for a canonical transition compiler. Reuse it rather than inventing a second diff representation.

## Target model

Introduce one application-internal transition operation, conceptually:

```text
load + validate old Config
load + validate candidate Config
          |
          v
classify_config_transition(old, candidate)
          |
          +-- invalid -> reject before mutation/publication
          +-- no-op
          +-- live-reloadable diff
          +-- restart-required fields
          +-- optional warnings/compatibility diagnostics
```

Names may differ. The important properties are:

- the field disposition table has one authority;
- `reload.rs` and operator mutation/application consume the same classifier;
- transition classification is pure/deterministic and testable without sockets/processes;
- publication and process restart remain separate execution mechanisms after classification.

## Governing constraints

1. Do not change public TOML keys/defaults merely to simplify the classifier.
2. Preserve `serde(deny_unknown_fields)` behavior and current semantic validation.
3. Preserve credential validation boundaries; never print or include raw credentials in transition diagnostics.
4. Preserve atomic mutation: invalid edited bytes never replace the original file.
5. Preserve atomic rehash: invalid/staging-failed candidates never replace the current generation.
6. Preserve restart-required behavior. A restart-required candidate must not partially apply its reloadable subset.
7. Preserve no-op behavior and digest checks.
8. Preserve old-generation leases and retirement semantics; the classifier must not own runtime publication.
9. Do not make the config module aware of Axum, control sockets, process management, or generation internals.
10. Do not add a generic diff library or reflection framework. The explicit typed field policy is appropriate for this project.
11. Do not deserialize and rewrite the complete TOML document for CLI mutations; preserving comments/unrelated formatting remains a valid reason for the bounded text editor.

## Workstream A — Inventory all configuration transition entry points

Trace at minimum:

- startup `Config::from_toml` / credential validation;
- `check-config`;
- `set`, `dashboard public`, `newkey`, connect/logout, onboarding, and editor flows;
- `operations/config_mutation::apply_after_mutation`;
- control protocol `reload_config` requests;
- `reload.rs` candidate load/digest/stage/publish flow;
- runtime-generation factory construction;
- tests that assert restart-required field names and rehash dispositions.

Document the call graph in the implementation PR/commit notes if useful, but do not add a permanent generated graph.

## Workstream B — Define the canonical transition result

Build the result around the existing `FieldDisposition`, `ReloadableConfig`, and `ReloadDiff` concepts rather than replacing them wholesale.

The canonical result should be able to express at least:

- unchanged;
- reloadable changed sections/fields;
- restart-required field paths;
- warnings already owned by reload policy;
- candidate typed config/reloadable snapshot as needed by the caller.

Keep stable field paths suitable for CLI/control responses. Prefer one helper such as `classify_transition(&Config, &Config)` over several independently maintained `changed_restart_fields`, `changed_reloadable_sections`, etc.

If full `Config` equality is not semantically appropriate because credentials/runtime-resolved values are excluded, make that exclusion explicit in the transition API and tests.

## Workstream C — Make rehash use only canonical classification

Refactor `reload.rs` so its flow is visibly:

1. load bounded candidate bytes/config;
2. validate candidate and content digest;
3. classify old -> candidate through the canonical transition authority;
4. return no-op or restart-required before building a replacement generation;
5. construct/stage the replacement only for a live-reloadable candidate;
6. atomically publish;
7. retire the old generation under existing lease/task rules.

Do not let `reload.rs` reproduce a separate list of restart-required keys.

## Workstream D — Make mutation application consume the same authority

After Plan 180 removes the runtime callback, refactor `operations/config_mutation.rs` so mutation mechanics and apply mechanics remain separate:

- text-edit functions produce bounded candidate bytes;
- candidate bytes are typed/validated before atomic replacement;
- where both pre-edit and post-edit config are available, classify the transition once and carry the result forward;
- `ApplyMode::LiveOrReport` uses canonical disposition rather than relying on a later server round-trip to discover obvious restart requirements;
- the server/control path still revalidates/reclassifies independently before publication because the on-disk file may change between mutation and control execution;
- restart execution goes through the operations lifecycle service from Plan 180.

The client-side classification is an operator optimization/diagnostic, not a trust boundary. Server-side rehash remains authoritative for live publication.

## Workstream E — Reduce configuration module duplication where natural

Only after the transition contract is stable, consider small mechanical splits of `config.rs` if they improve navigation, for example:

```text
config/
  mod.rs
  types.rs
  defaults.rs
  load.rs
  validation.rs
```

This is optional. Do not turn dozens of cohesive configuration structs into one-file-per-struct modules. The primary acceptance criterion is one transition policy, not a smaller `config.rs` byte count.

## Workstream F — Strengthen deterministic transition tests

Add table-driven coverage for representative transitions:

- identical config -> no-op;
- one reloadable scalar;
- a whole atomically reloaded mapping such as provider/model-router configuration;
- one restart-required scalar;
- simultaneous reloadable + restart-required changes -> entire transition restart-required, no partial live application;
- unknown/invalid fields -> validation failure before classification;
- secret changes do not leak values into debug/display/result payloads;
- digest mismatch/re-read race remains rejected;
- failed generation build leaves old generation authoritative.

Existing runtime lifecycle R011–R013 and operations/config tests should be reused rather than replaced.

## Focused verification

```bash
cargo test --manifest-path rust/Cargo.toml --test operations_o004 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test operations_o005 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r011 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r012 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test runtime_lifecycle_r013 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
```

Then run the roadmap-wide verification baseline.

## Acceptance criteria

- There is one typed field-disposition/transition authority for current -> candidate config.
- `reload.rs` does not maintain an independent restart/reload key list.
- operator mutation paths and live rehash consume the same classification semantics.
- operations no longer depend back on `runtime` to execute restart-after-mutation.
- invalid candidates cannot mutate disk or runtime authority.
- mixed restart-required/reloadable changes never partially publish.
- transition diagnostics do not expose credentials.
- public config keys/defaults and current runtime behavior are unchanged.
- full Rust and tooling suites remain green.

## Handoff note

Do not over-generalize the transition representation. An explicit typed classifier over EggPool's configuration is safer and more maintainable than reflection/dynamic path walking for a configuration surface of this size.