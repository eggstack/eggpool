# O004 — Config, Key, Provider Onboarding, and Live-Apply Mutations

Status: queued behind O003

Source roadmap: `migration-rs/subsystems/operational-cli-lifecycle-roadmap.md`

Primary class: capability/invariant

Hard dependency: accepted O003.

## Objective

Implement the Rust command handlers and reusable mutation services for `init-config`, `edit`, `set`, `getkey`, `newkey`, `connect`, `connect list`, `logout`, `onboard`, and `dashboard public`, while preserving validation, secret handling, provider-template behavior, and the Python live-rehash/restart decision contract.

## Architecture

Create a small `operations::config_mutation`/provider-onboarding boundary. CLI code should parse/prompts/render; mutation services should accept explicit paths/inputs and return typed outcomes.

Do not build a generic configuration-management framework. Only support mutations exposed by the existing commands.

## Atomic file mutation

For commands that modify TOML or related local files:

- resolve the canonical config path once;
- read with bounded size and reject malformed input before mutation;
- preserve unrelated sections/values and comments to the extent frozen by O001;
- write to a sibling temporary file with restrictive reviewed mode;
- validate the complete candidate config before rename;
- flush/sync as appropriate, then atomic rename on supported local filesystems;
- preserve existing owner/mode where possible;
- remove temp files on failure;
- never truncate the original first.

If exact comment-preserving edits cannot be safely achieved with the current TOML parser, implement a narrow section/key editor patterned on the Python `toml_edit` behavior. Do not add a broad document framework unless O001 proves preservation semantics require it.

## `init-config`, `edit`, `set`

### `init-config`

- choose target exactly as oracle;
- refuse overwrite unless `--force`;
- use the bundled current example/template source;
- create parent dirs only where Python does;
- validate generated config before success;
- no secret material is seeded beyond documented placeholders.

### `edit`

- preserve `$EDITOR`/`$VISUAL` and fallback editor ordering;
- use direct process exec/spawn without shell interpolation;
- distinguish no editor from editor process failure;
- do not automatically rehash merely because an editor exits unless Python does.

### `set key value`

- freeze supported dotted-key syntax and type coercion from O001;
- reject unknown/unsafe keys exactly as current authority;
- validate resulting config before atomic replace;
- apply live through O003 rehash when allowed and running;
- surface restart-required rather than pretending live success.

## Server key commands

`getkey` and `newkey` intentionally touch secret material.

Requirements:

- resolve inline vs env-indirected API key exactly;
- `getkey` prints only on the explicit command surface and never logs/traces the value;
- `newkey` uses cryptographically strong randomness through an existing audited Rust primitive; no custom PRNG;
- preserve `api_key_env` semantics: do not silently write an inline key when the env directive owns the value;
- old-key output remains redacted unless `--show-old` explicitly requests the current contract;
- file mode/atomic validation rules apply;
- live-apply/restart behavior follows O001.

Any secret wrapper should redact `Debug`/error display by default.

## Provider templates and connect

Port provider-template loading/validation needed by onboarding. Prefer embedding reviewed template files using `include_str!`/resources rather than introducing runtime package-discovery complexity.

`connect` must cover:

- provider selection by id/name and optional template path;
- `connect list` status/priority annotations;
- account naming/defaults;
- auth field/env-var choices supported by templates;
- proxy/wire/provider structural fields;
- duplicate account/provider behavior;
- interactive cancellation/EOF;
- non-echoed secret input where the terminal supports it;
- validation before commit;
- no network/provider probe unless the existing Python command contract requires it;
- apply via rehash when running, with the same control-unavailable/restart fallback semantics.

Avoid a TUI/dialog library; a small stdin/stdout prompt abstraction with injectable test input is sufficient.

If hidden terminal input cannot be implemented safely without a tiny terminal crate, justify a focused dependency rather than echoing credentials or importing a general interactive framework.

## `logout`

Support target matching by provider/account/env/key identifiers exactly as O001, including ambiguous interactive selection.

- remove only the selected account/provider fragment;
- never print the removed secret;
- preserve unrelated provider config;
- validate after mutation;
- live apply when possible;
- control-unavailable outcome must be explicit and actionable.

## `onboard`

Build onboarding as composition of existing O004 operations, not a separate mutation engine.

Freeze the Python flow/order: provider connection, config/key readiness, validation, optional/start behavior, cancellation, and already-configured cases. A failed late step must leave earlier successfully written config valid and tell the operator what remains; do not attempt unsafe blanket rollback of user-entered provider credentials.

## `dashboard public`

Implement the current command behavior exactly: projection/toggle semantics from O001, config mutation if any, API-key/public URL safety, and rehash/restart decision. Never expose a dashboard URL containing embedded secrets.

## Live-apply decision service

Centralize post-mutation behavior so connect/logout/newkey/set/dashboard/onboard do not each reinvent control logic.

Typed outcomes should distinguish:

- server not running;
- rehash applied/no-op;
- restart required;
- control unavailable/timeout/protocol failure;
- restart fallback succeeded/failed where the oracle authorizes fallback.

A failed rehash must not cause an unconditional restart unless the specific Python command does so.

## Tests

Use temp HOME/XDG roots and deterministic prompt input. Cover:

- config create/force/invalid target;
- editor resolution/path spaces/no editor;
- set scalar/list/bool/string/invalid/unknown fields;
- atomic failure before/after temp write;
- inline/env API key behavior and redaction;
- newkey entropy shape without snapshotting actual value;
- provider list/custom template/invalid template;
- connect duplicate/cancel/secret input/env/proxy/wire cases;
- logout direct/ambiguous/cancel/missing;
- unrelated TOML preservation;
- live apply applied/noop/restart-required/control unavailable/server stopped;
- simultaneous mutations serialize or fail cleanly rather than lost-update;
- onboarding fresh/existing/cancel/late-start failure;
- dashboard public behavior;
- no secret in stdout/stderr except explicit getkey/newkey/print-old contract.

## Non-goals

- configsetup agent renderers (O005);
- generic remote configuration;
- provider inference verification;
- password vault/keychain integration;
- dashboard redesign.

## Verification

Run fmt/Clippy, focused O004 tests, F003 config/CLI tests, R005/R007/R013 reload regressions, O002/O003 lifecycle tests, targeted Python connect/config/onboard/key tests, migration oracle, and static checks.

## Closure evidence

Write `migration-rs/closure/operations/004-status.md` with mutation matrix, atomicity/fault results, live-apply outcomes, secret audit, dependency changes, and unresolved findings.

## Acceptance criteria

O004 closes only when all owned config/key/provider/onboarding commands have real Rust behavior, failed input or control/provider-local surprises cannot corrupt config or expose credentials, mutations preserve unrelated user config, and post-mutation runtime convergence follows the frozen Python contract.

Accepted O004 promotes only O005.