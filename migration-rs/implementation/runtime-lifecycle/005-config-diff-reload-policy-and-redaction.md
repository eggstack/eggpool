# R005 — Config Diff, Reload Policy, and Redacted Change Model

Status: queued; depends on accepted R004 closure

Source roadmap: `migration-rs/subsystems/runtime-lifecycle-roadmap.md`

Primary class: invariant/capability

## Objective

Port the Python live-reload classification into Rust as one exhaustive, fail-closed, test-visible policy. R005 decides whether a validated candidate config is identical, live-reloadable, or restart-required and produces a secret-safe typed diff. It does not publish a generation.

## Policy types

Introduce explicit types equivalent to:

```text
ReloadDisposition = Live | RestartRequired | Ignored
ConfigChange {
    path,
    disposition,
    section,
    old_display,
    new_display,
    secret,
}
ConfigDiff {
    changes,
    live,
    restart_required,
}
```

Names may differ. Do not represent policy only as ad hoc `if old.foo != new.foo` branches inside the future reloader.

## Source of truth

R001's exported Python field-disposition/redaction fixture is authoritative.

Port all currently supported Rust `Config` fields into a reviewable table/rule set. The default for any field/rule not explicitly recognized is `RestartRequired`.

The implementation must include a schema-coverage test that fails when a new Rust config field is added without a reload classification. Use a deterministic serialized/default-config projection or another stable mechanism; do not depend on fragile source-code regex parsing if serde metadata can provide the needed projection.

## Required classifications

Preserve the Python contract, including examples such as:

### Restart-required / constructor-owned

- listener host/port;
- server API key/auth constructor settings;
- server thread/log/access-log constructor settings;
- DB path/WAL/synchronous/worker settings;
- process transport/global network constructor settings where Python marks them restart-required;
- middleware/security route-topology settings;
- trace-writer queue/batch constructor state;
- dashboard route enable/public/theme topology where classified restart-required;
- other R001 restart-required paths.

### Live / generation-or-task-owned

- provider/account configuration;
- model overrides/capabilities and model-router mapping;
- routing strategy/fairness/retry/wire-negotiation/trace-guard settings;
- generation-owned transcoder/finalization policy;
- live model exposure/staleness settings;
- `server.max_request_body_bytes`;
- maintenance/retention settings consumed by active-generation task ticks;
- process task intervals/enabled state explicitly classified live;
- other R001 live paths.

Do not broaden live reload just because Rust can rebuild a component easily; parity policy is deliberate safety behavior.

## Dynamic maps and ordering

Providers/accounts/model routers and other nested maps require stable diff paths.

Requirements:

- added/removed/changed map entries produce deterministic paths/sections;
- secret fields within dynamic entries are redacted;
- array/list ordering follows semantic Python behavior rather than incidental Rust map iteration;
- changed-section summaries are stable and deduplicated;
- no full serialized config is used as operator-facing diff output.

## Secret redaction

At minimum redact API keys/tokens/password/proxy credentials and any R001 secret path. Secret changes render only a neutral marker such as `<changed>`.

Tests must search `Debug`, `Display`, serialized diagnostics, and reload result projections for seeded secret sentinel strings.

## Digest/no-op behavior

Support the content-digest contract used by rehash:

- same validated digest and semantic config -> no-op;
- expected digest mismatch -> typed stale/digest mismatch before mutation;
- formatting/comment-only changes that do not alter semantic config should follow the Python result frozen in R001 (do not guess);
- a no-op does not allocate a candidate generation or bump generation/publication epoch.

R007 owns the actual file read/validation/expected-digest transaction; R005 provides pure policy functions.

## Mixed changes

A diff containing any restart-required path is not partially live-applied.

Return the complete restart-required path set, plus redacted live changes for diagnostics, but classify the transaction as restart-required before candidate build/publication.

Ignored paths, if any, must be retained in audit/result semantics exactly as R001 freezes them; do not silently discard unknown paths as ignored.

## Tests

Required tests:

- exact R001 path -> disposition parity for the complete exported table;
- coverage assertion for every Rust config field/rule;
- unknown synthetic path defaults restart-required;
- live-only mutation set returns live;
- restart-only mutation set returns restart-required;
- mixed set rejects partial live semantics;
- providers/accounts add/remove/change produce stable paths;
- model-router dynamic keys remain deterministic;
- secret old/new values never appear in `ConfigChange` display/Debug/JSON;
- identical semantic config is no-op;
- changed sections/order match Python fixture cases.

Property-style loops over the fixture are preferred to hundreds of copy-pasted tests; do not add a property-testing dependency solely for this.

## Scope boundaries

R005 must not:

- build or publish a candidate generation;
- write config-derived DB rows;
- reschedule tasks;
- change handler authority/body limits yet;
- implement CLI/control socket;
- add config fields for Rust convenience.

## Verification

```text
cargo fmt --all -- --check
cargo clippy --all-targets -- -D warnings
cargo test --all-targets
cargo test --test <R005 reload-policy tests>
uv run pytest tests/migration_rs -q --tb=short --maxfail=1
uv run pytest <Python config reload policy/diff tests> -q --tb=short --maxfail=1
git diff --check
```

## Acceptance criteria

R005 closes only when:

- every Rust config field is explicitly covered or fail-closed by a tested rule;
- path/disposition/section semantics match R001;
- secret values cannot enter diff/diagnostic output;
- dynamic-map diffs are deterministic;
- mixed diffs cannot be partially live-applied;
- pure policy functions are independent from publication/task/DB mutation.

## Closure

Write `migration-rs/closure/runtime-lifecycle/005-status.md` and promote R006.
