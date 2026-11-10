# Application Boundary Milestone 003 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/application-boundary/003-local-metadata-profile.md`

Source subsystem roadmap:

- `plans/subsystems/application-boundary-roadmap.md#m003--local-metadata-profile-and-doctor`

Repository baseline reviewed: `e049025274a397d7799c3ae21ed106947c2a1572`

Implementation commits:

- Uncommitted working tree on that baseline. No commit has been created.

## 1. Executive finding

M003 is complete. `rust/config.local.example.toml` turns off EggPool-owned
model-info sources, pricing catalogs, and the update checker.
`eggpool check-config --local` fails while any of those keys is enabled and
names the key. Plain `check-config` still accepts today's defaults,
including OpenRouter model-info. The operator's provider catalog is not
treated as an EggPool metadata source. No new outbound client, reload
class, or firewall was added.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Local example parses with no violations | `local_example_has_no_outbound_metadata_sources`; `local_metadata_profile_names_external_sources_and_keeps_defaults` | pass | Path `rust/config.local.example.toml`. |
| OpenRouter model-info failure names the key | Same tests flip `model_info.sources.openrouter.enabled` | pass | |
| Pricing catalog failure names the key | Flip `pricing.catalogs.opencode_zen.enabled` | pass | |
| Update checker failure names the key | Flip `update_checker.enabled` | pass | |
| Provider catalog is not a violation | `local_metadata_violations` does not read `model_info.sources.provider_catalog` | pass | The example leaves that source enabled. |
| Default example still has OpenRouter model-info on | `Config::default()` has exactly `model_info.sources.openrouter.enabled`; `rust/config.example.toml` still sets `[model_info] enabled = true` | pass | `--local` is opt-in. |
| Error text hides a dummy secret | Tool-switch tests cover secret echo for config errors. The local-check message is the key list only | pass | `check_config` prints `local profile rejects outbound metadata or update sources: {keys}`. |
| CLI flag parses | `audit_export_and_local_check_parse_without_tool_commands`; contract matrix `check-config` options `["--local"]` | pass | |
| No reload-class change | `config_reload_policy.rs` diff is empty | pass | |

## 3. Production implementation evidence

`local_metadata_violations` returns a static key name for each enabled
source among:

- `model_info.sources.openrouter.enabled`
- `model_info.sources.artificial_analysis.enabled`
- `model_info.sources.huggingface.enabled`
- `pricing.catalogs.openrouter.enabled`
- `pricing.catalogs.opencode_zen.enabled`
- `update_checker.enabled`

`check_config` runs that scan only when `--local` is set, after the
existing validation. Success with `--local` prints the existing success
line and `Outbound metadata and update sources are off.` The check does
not rewrite the file and does not open a socket. `CheckConfig` is
`CheckConfig(CheckConfigArgs { local: bool })`.

The dedicated example sets `model_info.enabled` false, the three external
sources false, both pricing catalogs false, and `update_checker.enabled`
false. It leaves `provider_catalog` enabled. Comments say the file does
not block an operator-configured provider.

`rust/config.example.toml` gained a comment pointing at the local example.
Defaults are unchanged. The runtime manifest sha256 for that file is
`b3bc53a9d7063471c19a0d3d41ad2e6746e6f41d07e256490aba896afb585794`.
`build_manifest` passed after that update. The local example is a repo
file and is not embedded by `rust/build.rs`.

## 4. Verification executed

### Commands run

Local, offline, not CI. `cli_contract` and the config unit tests were not
invoked as separate commands. They passed inside both full serial suites.

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --offline -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --offline -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features --offline -- --test-threads=1
```

### Results

- `cargo fmt --check`: exit 0.
- `cargo clippy` with `-D warnings`: exit 0.
- Default serial suite: exit 0, elapsed 346s. `cli_contract` 7 passed. `build_manifest` 4 passed. Config unit tests passed inside the library target.
- No-default serial suite: exit 0, elapsed 415s.

## 5. Invariant review

A config that omits the local profile keeps OpenRouter model-info enabled.
`classify_transition` is unchanged. The doctor prints key names, not the
config struct, an API key, a fetched body, or a prompt. No outbound client
or background task was added.

## 6. Failure and recovery review

The check is read-only. Two concurrent checks do not write. A missing
config file uses the existing check-config error. Restart behavior is
unchanged. Keys that were already restart-required stay restart-required.

## 7. Migration and compatibility review

No schema and no default change. `--local` is optional. Existing systemd
units keep working. Copying the example and restarting is an operator
action. The check does not rewrite the file.

## 8. Security review

Failure text is the static key list. The loaded config is not printed.
Provider base URLs are not blocked.

## 9. Documentation and operations

Updated: `docs/configuration.md`, `docs/cli-reference.md`,
`docs/deployment.md`, and the pointer comment in `rust/config.example.toml`.
The example path is `rust/config.local.example.toml`.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| low | The local example is not in `runtime-manifest.json` or `build.rs` | Release packaging does not embed it. Operators copy it from the repo | None for this milestone. The file is the documented profile. |
| low | Working tree is uncommitted | Closure SHAs are not yet a commit | Commit when requested. |

## 11. Roadmap disposition

Milestone closed. No successor is registered. The profile does not claim
an air-gapped update mirror.

## 12. Registry updates

Application-boundary M003 leaves the dependency-ready table. The roadmap
row points at this record. Nothing else was blocked on M003.
