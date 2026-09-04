# Foundation Milestone F003 — Configuration and CLI Compatibility Foundation

Status: closed; see [closure record](../../closure/foundation/003-status.md)

Repository baseline: `0bb5aaf419e60eadebaf3cce341a2ae4e3852e6c`

Source roadmap: `migration-rs/subsystems/foundation-roadmap.md#F003`

Primary class: capability

## 1. Objective

Port EggPool's configuration parsing/defaulting/validation/path resolution and establish the complete Rust CLI parser surface so existing config files and command invocations can be qualified before underlying service subsystems are ported.

## 2. Dependencies

Hard: F001 and F002 closed. Interface: none.

## 3. Python oracle evidence

Primary sources include `src/eggpool/models/config.py`, `config.py`, config validation/utils/reload policy, deploy-user/path helpers, `cli.py`, `cli_full.py`, `cli_exit_codes.py`, existing config examples/docs, and CLI/config tests.

## 4. Invariants

- same supported TOML field names/defaults/aliases/environment indirections;
- same config resolution precedence: explicit flag, environment, user config, local fallback as currently documented;
- validation is fail-closed for mutually exclusive proxy fields, malformed provider URLs/headers/wire paths, credentials, model routers, and other existing constraints;
- no secret values are printed in errors/help;
- full command/option hierarchy is represented even when implementation behind a command is intentionally not yet available;
- unavailable Rust internals must fail explicitly, not fall through to Python or silently succeed.

## 5. Scope

### In scope

Serde/TOML config structs, defaults, env resolution, validation, path helpers, config template loading as needed, CLI Clap command tree/options/help, migration-stage unavailable-command behavior, exit-code mapping foundation, differential config/CLI corpus.

### Out of scope

Implementing database-backed commands, provider connect network calls, serve, stats queries, backup/recovery, update/install, or other command internals owned by later milestones.

## 6. Required production changes

Use explicit typed validation after deserialization rather than attempting to encode all Pydantic cross-field rules into Serde attributes. Keep raw credential access narrow and redact Debug/output where needed.

The CLI parser should preserve command names, aliases, flags, defaults, repeatability, and option placement. Help wrapping/spacing may be semantically normalized only where terminal-width/framework differences are proven non-contractual; command/option presence and descriptions remain reviewable.

Commands whose implementation is deferred should return a migration-stage typed `not implemented in Rust candidate` error only in migration builds/tests. Do not expose that behavior as a final cutover contract.

## 7. Work packages

A. Port config data model and defaults.

B. Port validation and environment/path resolution.

C. Build full Clap parser tree from the contract inventory.

D. Add exit/error classification compatible with EggPool-owned CLI semantics.

E. Run differential valid/invalid config corpus and parser/help/argument corpus.

F. Document any Python incidental behaviors intentionally not copied; escalate material differences to ADR.

## 8. Failure/restart/contention

Config parsing is local/synchronous except file I/O. Partial config edits must never be written by read/validate operations. No runtime rehash is implemented here.

## 9. Compatibility/migration

Existing `config.toml`, `.env`, provider templates, and config path conventions must remain usable. This milestone must not introduce a Rust-only config file.

## 10. Tests

- defaults for every top-level config section;
- valid representative provider/account/proxy/wire/model-router configs;
- invalid mutually exclusive fields and missing env secrets;
- URL/header/path validation;
- config resolution precedence;
- CLI command/option inventory parity;
- help/unknown command/unknown option/missing argument exit behavior;
- secret redaction;
- regression corpus from Python config tests selected by contract value.

## 11. Verification commands

Rust fmt/clippy/test; migration differential config/CLI suite; targeted Python config/CLI tests. Broader Python suite only if shared Python fixtures/helpers were modified.

## 12. Documentation

Update Rust migration developer docs with config invocation examples and current command implementation status; do not fork user-facing config docs yet.

## 13. Acceptance criteria

The Rust candidate accepts/rejects/defaults the supported config corpus equivalently and exposes the same CLI parser surface with compatible EggPool-owned exit/error classes. Deferred command internals are explicit.

## 14. Stop conditions

Stop on an unresolved Pydantic behavior that changes accepted config meaning, a CLI ambiguity requiring product decision, or a request to implement downstream command subsystems inside this milestone.

## 15. Closure evidence

Differential matrix, config-field/CLI-command inventory coverage, exact failing supported differences if any, Rust dependency delta, and verification outputs.

## 16. Handoff notes

Do not copy Pydantic class structure mechanically. Port the configuration contract.
