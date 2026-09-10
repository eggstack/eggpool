# K005 — Cross-Era Exact Version Transitions and Rollback

Status: queued; blocked on accepted K004

Source roadmap: `migration-rs/subsystems/cutover-roadmap.md`

Primary class: invariant/capability

Hard dependency: accepted K004.

## Objective

Prove and harden exact package transitions across the Python/Rust implementation boundary while preserving the same EggPool config, database, runtime paths and operator state.

The minimum accepted cycle is:

```text
supported Python release
  -> Rust cutover wheel candidate
  -> supported Python release
  -> Rust cutover wheel candidate
```

This cycle must use real built wheel artifacts/install-manager behavior in isolated environments, not simulated version strings.

## Contract

For every version marked switchable in the K001 installable release catalog:

- `eggpool update VERSION` targets exactly that normalized version;
- newer/older direction does not change semantics;
- Python-versus-Rust era is an artifact property, not a different command;
- config/data/database are not copied to a version-specific location;
- target incompatibility is detected before package mutation whenever possible;
- if post-install verification fails, previous exact version is restored through the same owning manager when feasible;
- failure never reports success while leaving an unknown version running.

## Test environments

Required package-manager classes:

1. uv tool — primary public install path;
2. pipx — existing supported public path;
3. ordinary isolated pip/venv — direct PyPI workflow.

Standalone raw Rust does not have a Python-era package manager to downgrade into automatically; its exact raw-version semantics are retained from O008/K004 and tested separately. A standalone user who wants to adopt package-managed cross-era transitions follows K006 adoption.

## Historical Python targets

Select at least:

- the final/newest Python-era release intended as the primary rollback target;
- one older representative Python release in the catalog where DB/config compatibility allows;
- any historical missing-PyPI/backfilled release needed to prove the K001 gap strategy.

Do not choose an old version solely because it is easy to install if it does not exercise the intended migration window.

## Part A — fresh state cycle

For each required manager:

1. create isolated HOME/XDG/data/runtime roots;
2. install the Python rollback release;
3. initialize/validate config and DB using normal commands;
4. add deterministic non-secret provider/config state;
5. write representative DB data using the Python runtime or accepted fixture helpers;
6. record config/env/DB hashes or semantic observations;
7. exact-update to Rust wheel candidate;
8. verify Rust native executable and version;
9. open/migrate/use the same DB and run bounded finite/stream loopback inference/operational commands;
10. exact-downgrade to Python target;
11. verify Python CLI/runtime opens the same config/DB and observes the Rust-era writes that are inside the rollback contract;
12. exact-update to Rust again;
13. verify final state convergence.

Never use DB reset, config rewrite, or fresh state on each leg.

## Part B — existing-realistic state cycle

Repeat the transition over at least one copy of a representative existing Python-era database/config from the Q003 compatibility corpus. This catches migration assumptions hidden by newly initialized state.

State observations should include:

- provider/account/model inventory;
- request/attempt/reservation counts and terminal state;
- usage/cost aggregates;
- migration checksum/version ledger;
- dashboard/read-only visibility;
- operator config keys and env indirection;
- backup/recover ability after each era change.

## Part C — manager-specific exact transitions

### uv tool

Prove exact reinstall changes the installed version constraint rather than leaving an old constraint that prevents future updates. Verify the exposed `eggpool` path remains stable enough for deployment references, or document and correct deployment use to resolve the manager's exposed executable path.

### pipx

Use its supported package-spec update/install behavior. Preserve the venv and exposed executable ownership cleanly. Verify `pipx list`/metadata agrees with `eggpool version` after each leg.

### ordinary pip

Use the owning environment Python. Verify `importlib.metadata.version("eggpool")` equals CLI version on both eras, `RECORD` is coherent, uninstall/reinstall works, and no raw updater mutation bypassed pip.

## Part D — exact/latest/no-op semantics

Required cases:

- current -> exact current: no destructive reinstall unless explicit repair policy says otherwise;
- current -> latest when already latest: no-op;
- old -> latest;
- new -> older exact downgrade;
- `vX.Y.Z` == `X.Y.Z`;
- nonexistent version;
- version exists on GitHub but not in installable catalog;
- yanked target according to K001 policy;
- target requiring unsupported Python version;
- Rust target with no compatible wheel for host;
- Python target with incompatible DB/config rollback classification;
- historical immutable fallback target if K001 permits one.

Exact requests must never silently resolve to a nearby version.

## Part E — failure and automatic rollback matrix

Inject deterministic failures at:

- before manager invocation;
- package download/resolution;
- manager returns non-zero;
- manager reports success but installed version is wrong;
- target CLI cannot execute;
- target `check-config` fails;
- target DB open/migration validation fails;
- service restart hook fails;
- health check fails;
- rollback manager invocation fails;
- rollback target installs but self-check fails.

For failures before package mutation, prior install stays untouched.

For failures after target mutation, automatic rollback must attempt the **previous exact catalog version through the same owning manager**, then verify it. If rollback itself fails, retain an explicit typed `rollback_failed` result plus previous/target version identities and manual recovery command. Do not delete user config/DB in an attempt to recover.

## Part F — cancellation/concurrency

Preserve O008's one-update-at-a-time behavior across manager paths.

Test:

- two concurrent exact updates: one owns transition, other returns `update_in_progress`/equivalent;
- cancellation while package manager runs does not launch a second updater or strand service state silently;
- stale transition lock/journal recovery is bounded and identity-checked;
- SIGINT/SIGTERM does not leave the CLI claiming completion before child manager outcome is known.

Do not hold a global process/runtime lock while waiting on package-manager network operations.

## Part G — install metadata coherence

After every successful leg assert:

- package manager metadata version == `eggpool version`;
- exposed executable belongs to expected environment/provenance;
- wheel RECORD/uninstall is coherent where applicable;
- installable catalog era matches observed executable type;
- no stale rollback/stage/update-lock files remain;
- manager's next exact transition works without manual repair.

## Part H — data safety

Capture before/after checks for:

- config bytes unless normal target commands intentionally modify them;
- `.env`/secret files not read into evidence;
- DB migration ledger and integrity check;
- backup restore smoke;
- runtime/socket/PID files cleaned across stops;
- file ownership/modes in isolated environment.

A package downgrade must never imply a DB downgrade/reset. If target cannot safely open current DB, reject it before package mutation.

## Historical PyPI gap qualification

If K001/K003 stages backfilled Python wheels, K005 must include at least one such wheel in the actual cycle before it can be classified switchable.

If immutable VCS/archive fallback is used instead, verify uv/pipx/pip installation through the frozen immutable source and ensure `direct_url.json` records the immutable origin. No mutable branch/tag-only reference may be accepted as a guaranteed target.

## Tests

Create an explicit matrix runner with bounded artifacts and results rather than three separate ad hoc scripts.

Required output fields:

- manager class;
- source version/era;
- target version/era;
- result;
- rollback result when applicable;
- CLI/package metadata version;
- config/DB observation hashes;
- elapsed scalar;
- bounded error category;
- no raw subprocess body/environment secrets.

## Verification

Run K001-K004 focused tests, full Rust/migration/smoke, and every supported manager matrix available on the qualification host. Linux package-manager transition must be rerun in the K006/K007 environments before cutover.

## Closure evidence

Write `migration-rs/closure/cutover/005-status.md` with:

- implementation commits;
- exact matrix and artifact identities;
- Python -> Rust -> Python -> Rust results by manager;
- data/DB/config preservation evidence;
- rollback fault matrix;
- concurrency/cancellation results;
- historical-gap target evidence;
- unresolved findings;
- registry transition.

## Acceptance criteria

K005 closes only when:

- cross-era exact switching works through uv tool, pipx and ordinary isolated pip for the frozen supported window;
- manager metadata and CLI version never diverge after success;
- config/DB state survives the full cycle;
- invalid/incompatible targets fail before destructive mutation;
- post-mutation failures trigger bounded exact rollback;
- rollback failure is explicit and recoverable rather than hidden;
- no raw replacement runs on package-managed installations;
- no unresolved high/medium version-transition/data-loss finding remains.

Accepted K005 promotes only K006.