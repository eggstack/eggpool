# K007 — Deployed-Service Cross-Era Transition and Recovery

Status: conditionally closed; see [closure record](../../closure/cutover/007-status.md)

Source roadmap: `migration-rs/subsystems/cutover-roadmap.md`

Primary class: invariant/capability

Hard dependency: accepted K006.

Operational dependency: disposable rootful Linux environment comparable to accepted Q006.

## Objective

Prove that an already deployed EggPool service can change package/runtime era without requiring deployment-file rewrites, data relocation, database reset, or manual process repair.

The required real operational cycle is:

```text
Python package/service
  -> Rust wheel/service
  -> Python package/service
  -> Rust wheel/service
```

on preserved configuration/database state.

## Scope

Exercise the M9/O009 deployment contract against the M11 package-manager transition engine:

- personal systemd installation;
- production systemd installation where practical/qualified;
- service stop/start/restart;
- package update/downgrade;
- health/readiness;
- config validation and DB open/migration;
- cron/logrotate/backup integration where paths reference the EggPool executable;
- failure recovery.

Do not redesign the deployment model.

## Stable executable-path contract

Inspect how current systemd/cron deployment artifacts resolve `eggpool`.

The cutover must ensure:

- a manager-owned exposed `eggpool` path remains stable across Python/Rust wheel replacement, or deployment artifacts resolve a stable launcher/symlink under the manager's control;
- service files do not point into an ephemeral version-specific venv path that changes on every reinstall unless the manager guarantees stability;
- personal and production deployment modes preserve current ownership/path safety;
- `ExecStart` after transition resolves the exact installed target version;
- a downgrade cannot accidentally start a stale Rust rollback binary or another manager's command.

If the current O009 unit hard-codes a path that is unstable under uv/pipx recreation, correct the unit builder narrowly and add migration/update handling. Do not introduce a permanent EggPool-specific launcher daemon.

## Pre-transition state capture

Before changing the package record:

- exact current version and era;
- install provenance/manager;
- service installed/enabled/active state;
- resolved executable path and inode/hash where useful;
- config path;
- DB path and integrity/migration state;
- runtime/PID/socket state;
- relevant deployment artifact hashes;
- backup location/policy without reading secrets.

Create a fresh backup only if this is already part of safe update policy or a bounded M11 enhancement. Do not make normal version switching depend on copying the entire data directory.

## Transition sequence

For a running service:

1. acquire the one update/transition ownership guard;
2. resolve/validate exact target and rollback compatibility;
3. stop through the existing service/process authority;
4. verify old process is actually gone before mutating executable package;
5. perform K004 exact package transition;
6. verify target version/provenance/config/DB;
7. start using the existing deployment authority;
8. wait boundedly for health/readiness;
9. verify runtime-status and one deterministic local request/operational command;
10. commit transition and remove temporary rollback state.

If the service was disabled/inactive before transition, preserve that state; do not start it merely because package update succeeded.

## Failure/recovery matrix

Inject or simulate:

- service refuses to stop;
- PID/socket state remains stale;
- package manager fails before mutation;
- target installs but version is wrong;
- config check fails under target;
- DB open/migration compatibility fails;
- service start fails;
- service starts then readiness fails;
- service process crashes during first health window;
- rollback package reinstall fails;
- old service fails to restart after rollback;
- deployment executable path resolves to another manager after update;
- update invoked concurrently with rehash/restart/another update.

Rules:

- never mutate package while old process still executes the managed path unless manager semantics and OS behavior are explicitly safe and tested; default is stop first;
- if target package mutation never occurred, restore prior service state only;
- if target package was installed but validation/start fails, exact-restore previous version via same manager and then restore previous service state;
- never delete config/DB as recovery;
- if rollback fails, leave service stopped unless the known installed target can be proven safe to run, and print exact manual recovery commands.

## Config/database preservation

Use one stateful fixture/deployment throughout the cross-era cycle. Verify after each leg:

- config file same canonical path and semantically identical unless an explicit command modifies it;
- `.env` ownership/mode preserved and contents not logged;
- database passes integrity and migration checks;
- Python can reopen post-Rust DB within K001 rollback window;
- Rust reopens post-Python DB;
- no duplicate migration/application occurs;
- backups remain recoverable.

## systemd personal mode

On disposable Linux:

- install Python package under the intended user manager;
- run O009 personal systemd installation;
- transition through all three legs;
- verify service user/group/path/environment loading;
- verify service remains enabled if initially enabled;
- verify logs/runtime paths ownership;
- uninstall/reinstall service remains idempotent after cutover.

## systemd production mode

Production mode uses the dedicated `eggpool` user and system locations. Determine the canonical package-manager install strategy for the production binary before mutation.

Do not rely on a root user's private uv/pipx environment accidentally being reachable by the service user. Options must remain simple and explicit, e.g. a system-owned package-managed tool path or a standalone raw Rust binary for production mode. ADR-0004 permits standalone raw assets, but K007 must define whether production deployment is package-managed or standalone and then qualify exact rollback accordingly.

If Python-era production deployments were package-managed differently, document/adapt them without moving `/etc/eggpool` or `/var/lib/eggpool`.

## cron/logrotate/backup references

Check deployment snippets and installed files that call `eggpool`:

- cron watchdog;
- backup cron;
- logrotate hooks if any;
- service unit.

They must resolve the canonical installed command after package transition. Avoid version-specific absolute paths unless manager stability is guaranteed.

## Package-manager environments under systemd

Ensure update commands do not depend on interactive shell initialization. Manager executable paths/HOME/XDG environment must be resolved explicitly for the deployment user. Do not source arbitrary shell rc files as root.

## Tests

### Deterministic/local

- unit rendering preserves stable manager path;
- inactive service remains inactive;
- enabled/active state restored;
- failed stop prevents package mutation;
- failed start triggers rollback;
- health failure triggers rollback;
- exact manual recovery emitted if rollback fails;
- concurrent update/restart conflict bounded;
- deployment artifact hashes unchanged where no rewrite required.

### Real disposable Linux

Run the complete Python -> Rust -> Python -> Rust cycle under real systemd. At minimum include personal mode; production mode is mandatory if M11 continues to claim it as supported at cutover.

Record sanitized OS/systemd/package-manager versions and exact candidate artifacts. Do not store host identity.

## Dependencies

No new runtime framework. Reuse O003/O009 process/deploy services and K004 manager transition code.

## Closure evidence

Write `migration-rs/closure/cutover/007-status.md` with:

- implementation commit(s);
- deployed path/manager decision;
- exact service transition sequence;
- real Linux Python -> Rust -> Python -> Rust results;
- state/config/DB preservation table;
- failure/rollback matrix;
- personal/production support disposition;
- cron/logrotate/backup path checks;
- unresolved findings;
- registry transition.

## Acceptance criteria

K007 closes only when:

- a real deployed supported Linux environment completes the cross-era cycle;
- deployment artifacts resolve the correct executable after each leg;
- service enabled/active state is preserved;
- failed target validation/start returns to the prior exact version/state when possible;
- config/DB paths and data remain intact;
- production mode has an explicit qualified install authority rather than root-user PATH accidents;
- no unresolved high/medium service cutover/data-loss/ownership finding remains.

Accepted K007 promotes only K008.
