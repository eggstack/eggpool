# K006 — Quick Installer and Existing-Install Adoption Cutover

Status: ready for handoff

Source roadmap: `migration-rs/subsystems/cutover-roadmap.md`

Primary class: capability/invariant

Hard dependency: accepted K005.

## Objective

Convert `scripts/install.sh` from the Python/source-oriented bootstrap into a small, package-channel installer for the Rust-backed `eggpool` wheel while preserving existing installations, configuration, database and deployment state.

The public one-line install must continue to work, but a normal remote install must no longer clone the repository or imply that Python application dependencies are the canonical runtime.

## Public install model

Preferred order:

1. if an existing trusted EggPool package-manager installation exists, adopt its manager;
2. otherwise if `uv` is available, install through `uv tool`;
3. otherwise if a compatible `pipx` installation is available, use pipx;
4. otherwise bootstrap `uv` through its standalone installer and use `uv tool`;
5. never fall back to `sudo pip`, system-site package mutation, or an unqualified source build.

The exact order may be adjusted in K006 if preserving the current documented pipx preference materially improves compatibility, but there must be one deterministic rule and no accidental double install.

## CLI for installer

Add/support at minimum:

```text
install.sh                     # latest stable
install.sh --version X.Y.Z     # exact
install.sh --version vX.Y.Z    # same exact target
install.sh --upgrade           # explicit transition of an existing install
install.sh --force             # reinstall/repair under the same selected manager
install.sh --help
```

If `--upgrade` is retained, define whether it means latest stable only or can combine with `--version`. Avoid multiple ways to express conflicting target versions.

Unknown flags remain exit 2.

## Existing-install adoption

When `eggpool` is already on PATH:

- do not simply return after printing `eggpool version`;
- query the Rust provenance logic added in K004 or a narrow install-provenance helper that can be called safely from the installer;
- if uv/pipx/pip owns the install, use that manager for the requested target;
- if it is a standalone Rust install and the user requested package-channel adoption, provide an explicit migration path rather than overwriting manager files on top of it;
- if it is ambiguous, fail with actionable instructions.

Do not select a manager only because its executable exists on PATH.

## Standalone-to-wheel adoption

Support a safe explicit migration from O008 standalone Rust binary to the canonical wheel installation:

1. identify standalone executable and current exact version;
2. record running/service state;
3. ensure package-manager target can expose `eggpool` without silently shadowing another path;
4. stop service if needed;
5. install exact/latest wheel through selected manager;
6. verify PATH resolution/provenance/version;
7. remove the old standalone executable only after the new manager-owned command is verified, or retain it under a deterministic rollback name outside PATH until transition commits;
8. restart/health-check if previously running;
9. restore standalone command on failure.

Do not make this path automatic when ownership is ambiguous.

## Python-install to Rust-wheel upgrade

This is the primary existing-user path.

For an existing uv/pipx/pip Python `eggpool` installation:

- preserve the same managed environment where practical;
- request the Rust cutover version/latest through that manager;
- preserve the XDG config/data/DB locations;
- do not delete Python-era app state;
- verify installed command is the Rust native executable after transition;
- service restart/health integrates with K005/K007 rules.

The package manager may remove Python application dependencies from its environment because the Rust wheel no longer needs them. That is expected and not data loss.

## Fresh install

A fresh supported-target install must:

- install a Rust wheel from PyPI/staging authority;
- create no repository checkout under `$HOME/eggpool` merely for runtime;
- seed config only if missing, using the same canonical XDG path and current example/minimal fallback behavior;
- never overwrite existing config or `.env`;
- show the same useful onboarding/deploy next steps;
- leave a bare `eggpool` command on PATH after shell path update;
- report exact installed version and manager.

## Python availability

Because the Rust wheel keeps `Requires-Python >=3.11` during M11 for rollback compatibility, the selected tool manager needs an appropriate Python environment even though EggPool runtime does not.

Prefer uv's managed-Python capability when a suitable interpreter is absent. Do not retain the current hard error that treats system Python 3.11-3.14 as an EggPool runtime requirement if uv can provision the packaging environment safely.

Pipx use remains subject to pipx's own Python requirements and fetch policy. Avoid writing a second Python installer.

## Source checkout path

Keep an explicit developer flow for a cloned repository. It may build/install the current Rust candidate through a local wheel or invoke documented Cargo tooling. It must be visibly distinct from the public PyPI installation path and must not accidentally install the latest PyPI version while a developer thinks they are testing checkout source.

Python oracle development remains supported separately through `uv run`/root project tooling until M12.

## Config seeding ownership

The current installer reads repository `config.example.toml` when available. A normal remote wheel install will no longer have a checkout.

Resolve this cleanly using one of:

- the Rust binary's existing `init-config` command as the canonical seeder after wheel installation; preferred if it reproduces current template/defaults;
- a small static installer fallback generated/validated against the canonical config example.

Do not duplicate a large stale config template in shell. Add a drift test if any shell fallback remains.

## PATH and manager collisions

Test cases:

- uv executable would overwrite a pipx-exposed command;
- pipx command conflicts with uv;
- old standalone binary precedes manager bin dir on PATH;
- manager exposes command through symlink/copy;
- custom `UV_TOOL_BIN_DIR`/pipx bin directory;
- shell PATH not yet updated;
- root/sudo invocation for personal install.

The installer should fail or give explicit recovery actions rather than silently changing which unrelated `eggpool` wins on PATH.

## Root behavior

Keep O009 root/deployment safety intact.

A normal personal quick install should not accidentally create a root-owned user package environment just because the curl pipeline was invoked under `sudo`. Refuse or require explicit system/deployment mode, matching existing safety posture.

Production system deployment remains `eggpool deploy systemd --install --production`, not a hidden branch in the quick installer.

## Network/security posture

- use HTTPS package/index/bootstrap URLs only;
- pin/verify the uv bootstrap source according to the current uv documented installer path; do not copy a mutable large third-party script into the repo;
- package integrity is provided by the package index/wheel hashes/attestations plus K008 release workflow;
- no provider API keys are needed;
- do not echo secrets from existing `.env`/config;
- bound diagnostic output from manager failures.

## Tests

Create a deterministic shell/fixture harness covering:

- fresh uv install from local wheelhouse/fake index;
- fresh pipx install when available;
- existing Python uv -> Rust adoption;
- existing Python pipx -> Rust adoption;
- exact `--version` downgrade/upgrade routing;
- existing config/DB byte/semantic preservation;
- no `$HOME/eggpool` clone on normal remote path;
- missing Python with uv-managed Python provisioning path;
- manager collision/ambiguous install refusal;
- standalone-to-wheel explicit adoption + rollback;
- source checkout installs local candidate, not PyPI latest;
- root personal install refusal;
- config seeding no-overwrite;
- unknown argument exit 2.

Use temp HOME/XDG/PATH and fake manager binaries where deterministic behavior is sufficient; complement with real uv/pipx staging tests from K005/K009.

## Documentation touched by K006

Update installer-local help/comments and developer documentation needed to test the new flow. Do not yet flip the public README/release claims before K010/K011.

## Dependencies

No Rust runtime dependency expected. Shell complexity should decrease relative to the existing installer.

## Closure evidence

Write `migration-rs/closure/cutover/006-status.md` with:

- implementation commits;
- before/after installer flow diagram;
- manager selection/adoption table;
- exact-version behavior;
- config/data/DB preservation evidence;
- collision/root/source-checkout tests;
- real staged install results;
- line/complexity change for installer as informational evidence;
- unresolved findings;
- registry transition.

## Acceptance criteria

K006 closes only when:

- fresh supported-target install selects a Rust wheel through the canonical package channel;
- normal public install no longer needs a repo clone;
- existing Python package installs upgrade in place under their manager;
- exact installer version selection works both directions where catalog allows;
- standalone adoption is explicit and rollback-safe;
- config/DB paths and contents are preserved;
- manager/PATH collisions fail safely;
- personal install cannot accidentally become root-owned;
- no broad new installer framework or runtime dependency is introduced;
- no unresolved high/medium install/adoption finding remains.

Accepted K006 promotes only K007.
