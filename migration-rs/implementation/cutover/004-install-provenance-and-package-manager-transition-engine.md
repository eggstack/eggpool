# K004 — Install Provenance and Package-Manager Transition Engine

Status: accepted; closed 2026-09-10

Implementation commit: `b33658be47d337c2c8a875b41d50ff436eb0f10b`

Closure record: [accepted closure](../../closure/cutover/004-status.md)

Source roadmap: `migration-rs/subsystems/cutover-roadmap.md`

Primary class: invariant/capability

Hard dependency: accepted K003.

## Objective

Replace the M9 assumption that every Rust executable can self-replace with an installation-aware update boundary that preserves package-manager ownership.

K004 determines how the current `eggpool` was installed and routes a requested exact/latest version transition to exactly one trusted authority:

- uv tool;
- pipx;
- ordinary pip/virtualenv;
- standalone Rust binary/O008;
- source/developer install;
- ambiguous/unmanaged install, which fails closed.

This plan does not change version compatibility policy; K001's installable release catalog remains authoritative.

## Core invariant

**Never directly overwrite an executable that is owned by a wheel/package-manager environment.**

The O008 verified executable replacer remains valid for a standalone Rust executable. It must not be used when `.dist-info`/manager state proves a wheel-managed installation.

## Part A — install provenance model

Add a small typed model such as:

```text
InstallProvenance
  UvTool { manager, environment, exposed_executable, package_metadata }
  Pipx { ... }
  PipEnvironment { python, environment, package_metadata }
  StandaloneRust { executable }
  SourceCheckout { root, executable }
  Ambiguous { evidence }
```

Exact names may differ. Keep it data-oriented; do not introduce a general package-management framework.

The model should retain only bounded safe facts needed for decisions. Do not surface full environment variables, site-packages listings, user names or secrets in diagnostics.

## Part B — provenance evidence hierarchy

Use corroborating installer-owned evidence. Expected sources include:

- current executable canonical path/symlink target;
- nearby environment interpreter and site-packages location;
- installed `eggpool-*.dist-info/METADATA`;
- `INSTALLER` when present;
- `direct_url.json` when present;
- uv tool environment/config/introspection where stable;
- pipx `pipx_metadata.json` / environment structure where stable;
- environment Python importlib metadata for ordinary pip installs;
- executable-format/version check for standalone candidate;
- source checkout markers only for explicit developer mode.

Do not infer uv versus pipx solely from `~/.local/bin/eggpool` or another exposed-bin directory. Both managers can expose commands there.

Manager-specific metadata may evolve. Encapsulate parsers behind narrow functions and fail ambiguous if signals conflict.

## Part C — transition authority

Introduce a service boundary such as `PackageTransitionService` that accepts:

- current provenance;
- normalized `ReleaseTarget`/catalog entry;
- current exact version;
- running/service state abstraction;
- bounded transition options.

It returns a typed transition result and never evaluates a shell command string.

### uv tool

Use a fixed argv equivalent to an exact tool reinstall, based on current uv behavior. Preserve the existing tool environment's relevant manager settings where safely discoverable. Exact version constraints must replace prior constraints when the operator explicitly asks for another version.

Do not manually mutate uv's tool environment with `pip`; uv documents tool environments as manager-owned and not intended for direct mutation.

### pipx

Use pipx's package-spec install/upgrade behavior with an exact requirement. Current pipx explicitly supports upgrade or downgrade when the installed version does not satisfy the supplied spec. Preserve recorded backend/Python/manager options where available.

### ordinary pip/venv

Invoke the interpreter belonging to the environment that owns the installed distribution:

```text
<env-python> -m pip install --upgrade --force-reinstall? eggpool==VERSION
```

Use the minimum flags required for deterministic target replacement. Do not invoke a random `pip` from PATH.

A system Python marked externally managed is not a supported mutation target unless the current EggPool distribution is already inside a safe owned environment. Fail with guidance instead of bypassing PEP 668 protections.

### standalone Rust

Reuse O008 `UpdateService`/verified raw asset replacement. This path still resolves the K003 raw artifact and uses the established staging/self-check/rollback rules.

### source checkout

Do not silently replace source checkout contents from the public package index. Return an explicit developer/source-install category and provide the appropriate documented command. `--from-source` keeps its explicit behavior if retained by current CLI contract.

## Part D — release target resolution

Before mutation:

1. normalize optional leading `v`;
2. resolve target through K001 catalog/release authority;
3. verify current target platform is supported;
4. verify package manager/Python compatibility for the target era;
5. verify DB/config rollback window if target is older/Python-era;
6. compute exact requirement/artifact source;
7. refuse unknown/non-catalog/mutable targets.

`latest` uses the latest stable compatible catalog/public release; prereleases remain excluded unless a later public contract adds explicit opt-in.

## Part E — process/service coordination

K004 should expose hooks required by K005/K007 but not reimplement process management:

- determine whether EggPool is currently running through O003/O009 state;
- stop only when the transition needs to replace the running executable/environment;
- preserve prior running/service state for post-install restore;
- avoid holding package-manager subprocesses under M8 runtime/reload locks.

K007 owns full systemd deployment transition qualification.

## Part F — bounded subprocess execution

Package-manager calls must:

- use an exact executable path determined from trusted provenance or an explicitly discovered manager binary;
- pass argv as structured arguments;
- cap stdout/stderr retained in memory/logs;
- set a bounded timeout appropriate for package download/install;
- use a minimal/allowlisted environment while retaining required HOME/XDG/PIPX/UV proxy/index configuration only when explicitly part of current manager state;
- never log index credentials, embedded URLs with credentials, authorization headers or arbitrary environment dumps;
- treat signal/cancellation as a failed transition with known postcondition.

Network access is to package/release authorities, not provider endpoints. Provider credentials must not be forwarded.

## Part G — transition journal

Do not add a DB schema. Retain a small process-local/temporary transition record while a mutation is in progress containing:

- prior exact version;
- target exact version;
- provenance class;
- whether the service was running;
- mutation phase;
- rollback availability;
- bounded error category.

This may be a private file adjacent to the install/runtime state if crash recovery across package-manager replacement requires it. If so, use create-new/private permissions, atomic updates, no secrets, and clear it only after final verification. K004 must justify whether persistent journaling is actually necessary; do not add it merely for architecture symmetry.

## Part H — self-check

After the package manager returns success, resolve the exposed executable again and verify:

- it still belongs to the expected manager/environment;
- `eggpool version` reports the exact target;
- binary/entrypoint era matches catalog expectation (Rust target should be native; Python target should resolve the historical package CLI);
- config can be read/validated under the target before service restart;
- DB compatibility precheck passes.

Full start/health/rollback belongs to K005/K007.

## Failure semantics

Typed failures must distinguish at least:

- ambiguous provenance;
- manager executable unavailable;
- manager metadata malformed;
- target incompatible with manager Python;
- target unsupported on platform;
- target unavailable/not catalogued;
- package-manager timeout;
- package-manager non-zero exit;
- post-install wrong version;
- ownership changed unexpectedly;
- standalone integrity/replacement errors inherited from O008;
- rollback required/available state.

Never convert ambiguous provenance into standalone self-update by default.

## Tests

Use isolated fake manager environments and deterministic executable fixtures. Tests must not modify the user's real uv/pipx installations.

Required cases:

- detect uv tool from corroborating metadata;
- detect pipx;
- detect ordinary venv pip;
- detect standalone Rust;
- source checkout explicit classification;
- same exposed bin directory with conflicting uv/pipx evidence -> ambiguous;
- missing `INSTALLER` but sufficient manager evidence still works;
- malicious/malformed `direct_url.json` cannot inject args/commands;
- exact argv contains one validated requirement;
- no shell interpolation;
- manager stdout/stderr/timeout bounded;
- unsupported/yanked/non-catalog target stops before subprocess;
- wheel-managed path never calls raw replacer;
- standalone path never invokes pip/uv/pipx;
- post-install wrong version fails closed.

## Dependencies

Prefer stdlib/filesystem/process parsing and existing serde/json support. No package-manager SDK crate is warranted.

## Verification

Run focused K004 tests plus O003/O008/O009 regressions, full Rust tests, migration tests and smoke.

## Closure evidence

Write `migration-rs/closure/cutover/004-status.md` with:

- implementation commit(s);
- provenance evidence/decision table;
- exact manager argv templates;
- security/environment/redaction policy;
- wheel-managed raw-overwrite negative proof;
- standalone O008 retention proof;
- focused/full verification results;
- unresolved findings;
- registry transition.

## Acceptance criteria

K004 closes only when:

- install provenance is deterministic or explicitly ambiguous;
- every supported package-managed install routes through its owner;
- standalone O008 remains isolated;
- no untrusted command-string execution exists;
- unsupported/incompatible targets fail before mutation;
- exact post-install version/ownership is verified;
- no package-manager metadata can be silently desynchronized by EggPool;
- no unresolved high/medium update-authority or command-injection finding remains.

Accepted K004 promotes only K005.
