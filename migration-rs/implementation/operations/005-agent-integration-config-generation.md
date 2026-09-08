# O005 — Agent Integration and `configsetup` Generation

Status: dependency-ready; O004 closure accepted

Source roadmap: `migration-rs/subsystems/operational-cli-lifecycle-roadmap.md`

Primary class: capability

Hard dependency: accepted O004.

## Objective

Implement all Rust `configsetup` commands using a shared bounded integration context and small target renderers while preserving O001 output, model-selection, secret-display, write/force, clipboard, and config-mutation behavior.

Targets at current main:

- OpenCode;
- Claude Code;
- Aider;
- Codex;
- Qwen Code;
- Kilo;
- Continue;
- Cline;
- Roo Code;
- Goose;
- OpenHands.

## Shared integration context

Build one reusable context from:

- resolved EggPool config;
- server host/port/base URL overrides;
- server API key/reference semantics;
- available model/catalog facts needed by the existing target;
- transcoder/protocol capabilities;
- target-specific model requirement;
- whether building the context mutated config/key/transcoder state according to the frozen Python behavior.

Do not make a provider network call merely to render a snippet unless O001 proves the Python command requires it. Prefer current persisted/catalog state.

## Rendering rules

Each target renderer should be a pure function over typed context where practical. Preserve:

- protocol/base URL/path shape;
- model id selection and collapsed/provider-suffixed behavior;
- environment-variable vs JSON/YAML/TOML shape;
- exact fields whose spelling is consumed by the target;
- newline/escaping/quoting semantics;
- default filenames/paste hints;
- secret placeholder vs real secret rules.

Do not introduce a general template engine. Handwritten small renderers or Serde structures are easier to audit.

## Secret handling

By default generated/displayed content must follow current redaction/placeholder behavior. Only `--print-secret` or target-specific existing explicit behavior may materialize the full key.

Secret-bearing rendered strings must:

- never be traced/debug-logged;
- not appear in error messages;
- not be retained in global caches;
- be dropped after output/write as ordinary local values;
- use restrictive file modes where O001 requires them.

`--no-clipboard` must suppress clipboard attempts. Clipboard discovery should use the existing platform commands (`pbcopy`, `xclip`, `xsel`) only where present, with bounded subprocess timeout and no shell interpolation. Clipboard failure is non-fatal where Python treats it as best-effort.

## Write/force behavior

For targets supporting shared options:

- no `--write`: render/print according to contract;
- `--write`: choose default or `--output` path;
- refuse existing target unless `--force`;
- create only reviewed parent directories;
- temp-write + atomic rename where possible;
- never overwrite an unrelated file after a render/validation error;
- preserve exact default file naming and paste hints.

If a target intentionally emits shell environment text rather than writing a structured app config, preserve that distinction.

## Model selection

Freeze target behavior from O001:

- when a model is required;
- how `--model` overrides are validated;
- how provider-suffixed/collapsed ids appear;
- what happens when no model is available;
- whether a target permits arbitrary model text or requires an exposed model.

Do not put M7 semantic model-router selection inside config generation. Virtual model ids are configuration values, not a prompt-time selection here.

## Config mutation side effects

Some Python integration-context paths can generate a server key or mutate transcoder compatibility and then restart/apply the config. If O001 confirms those effects remain current:

- use O004 mutation services, not duplicated file editing;
- report mutation explicitly;
- run the same post-mutation rehash/restart decision;
- never mutate merely because the target renderer was called if a no-mutation alternative is accepted by the current contract.

## Tests

Use golden/scalar fixtures from O001 for every target. Cover:

- default render;
- host/base URL override;
- required/explicit model;
- collapsed vs provider-suffixed model ids where applicable;
- native/transcoded protocol context;
- default redacted/no-secret output;
- `--print-secret` explicit output;
- write/default path/output override/force/existing file;
- no clipboard/clipboard success/failure/timeout;
- path spaces and shell escaping;
- missing model/key/config;
- any target-specific config mutation and runtime apply;
- no unrelated config mutation;
- deterministic output across repeated runs.

A single integration test should iterate all targets and assert every `ConfigsetupCommand` variant has an implementation, preventing future parser/render ownership drift.

## Dependency posture

No template engine, YAML framework, or clipboard crate is expected unless the exact target output cannot be generated safely with existing dependencies. Structured YAML-like outputs can be rendered directly if the frozen schema is tiny; do not add a broad serializer solely for one static snippet without justification.

## Non-goals

- detecting whether third-party agents are installed;
- editing arbitrary third-party application config locations beyond existing `--write` contract;
- launching agents;
- plugin framework;
- provider live tests.

## Verification

Run fmt/Clippy, focused O005 target matrix, O004 mutation regressions, F003 CLI parser tests, current Python integration/configsetup tests, migration oracle, and secret-scanning assertions over captured outputs.

## Closure evidence

Write `migration-rs/closure/operations/005-status.md` with all-target parity table, output/write/secret matrix, dependency review, and unresolved findings.

## Acceptance criteria

O005 closes only when every current `configsetup` parser target produces usable Rust output matching the frozen contract, explicit secret/write behavior is safe, and target generation does not duplicate config/runtime mutation architecture.

Accepted O005 promotes only O006.
