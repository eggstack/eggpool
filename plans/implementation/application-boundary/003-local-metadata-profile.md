# Application Boundary Milestone 003 — Local metadata profile and doctor

Status: implemented

Repository baseline: `e049025274a397d7799c3ae21ed106947c2a1572`

Source roadmap:

- `plans/subsystems/application-boundary-roadmap.md` M003

Long-term requirements:

- `plans/000-long-term-specification.md#1-product-definition`
- `plans/000-long-term-specification.md#6-non-goals`
- `plans/002-long-term-roadmap.md#phase-4--operations-integrations-and-deployment`

Applicable ADRs:

- `plans/adrs/ADR-0009-downstream-application-boundary.md` (accepted)

Primary class: capability

## 1. Objective

Ship a documented local profile and a `check-config` result that fails
while EggPool would contact an external model-info, pricing, or update
source. Inference against an operator-configured local provider stays
available.

## 2. Why this milestone is ready

No hard dependency. `rust/src/config_reload_policy.rs` already
classifies `update_checker.enabled` as restart-required. Model-info
and pricing keys already exist.

## 3. Current implementation evidence

- `ModelInfoSourcesConfig` in `rust/src/config.rs` defaults OpenRouter
  model-info to enabled. Artificial Analysis and Hugging Face default
  off. The example sets `[model_info] enabled = true`.
- `[pricing.catalogs.openrouter]` defaults off in
  `rust/config.example.toml`.
- `[update_checker] enabled = false` in the example, and the task is
  registered from `rust/src/task_supervisor.rs` only when enabled.
- `eggpool check-config` is the existing validation command in
  `rust/src/cli.rs`.
- Provider calls the operator configured, including a loopback
  OpenAI-compatible server, are the inference path and are not metadata
  fetches.

## 4. Invariants that must not regress

- A config without the local profile keeps today's defaults, including
  OpenRouter model-info, so this milestone is opt-in.
- `classify_transition` remains the only reload-versus-restart
  authority. Do not add a second key list.
- The doctor prints no API key, no fetched body, and no prompt.
- No new outbound client and no new background task.

## 5. Scope

### In scope

- One example profile, next to the existing examples, that turns off
  model-info sources, pricing catalogs, and the update checker.
- `check-config` accepts `--local` or an equivalent explicit switch
  documented in the CLI reference. Under that switch, any enabled
  model-info source, pricing catalog, or update checker is an error
  that names the key.
- A regression test with the profile and with one key flipped on.

### Explicitly out of scope

- Changing the default for existing installs.
- Vendoring model weights, blocking arbitrary provider base URLs, or
  claiming a fully air-gapped update mirror.
- Disabling a local or private provider the operator configured.
- Runtime behavior of catalog refresh against an operator's own
  provider. That is inference metadata from the configured upstream,
  and the doctor does not treat the operator's provider as an EggPool
  metadata source.

## 6. Required production changes

Add `rust/config.local.example.toml`, or a clearly named section in the
existing example if a second file would drift, containing the off
switches and a short comment that this is the profile for a gateway in
front of local models. Prefer a dedicated example file so the default
example stays the default.

`check-config --local` loads the same config resolution as today and
adds the source check. Failure lists the offending keys and exits
non-zero through the existing CLI error mapping. Success prints the
existing success line plus a single statement that outbound metadata
and update sources are off. Implementation lives in the config or
operations owner that already validates config, not in a new daemon.

Do not change reload classes. Keys that are already restart-required
stay restart-required. If a model-info enabled flag is live-reloadable
today, leave that classification; the doctor is a read-only check.

## 7. Ordered work packages

### Work package A — Profile

Intent: an operator can copy one file and get the off switches.

Required changes: the example file and a configuration-doc section.

Acceptance evidence: the file parses with `check-config --local`.

### Work package B — Doctor

Intent: a flipped switch fails the check.

Required changes: the flag and the key scan.

Acceptance evidence: tests for all-off success and one failure each for
OpenRouter model-info, a pricing catalog, and the update checker. The
failure text contains the config key and no URL query or secret.

### Work package C — Default unchanged

Intent: omitting `--local` preserves current validation.

Acceptance evidence: a config with default OpenRouter model-info still
passes plain `check-config`.

## 8. Failure, cancellation, restart, contention semantics

The check is local and read-only. It opens no socket. Restart is
unchanged. Two concurrent checks do not write. A missing config file
uses the existing check-config error.

## 9. Compatibility and migration

No schema and no default change. Existing systemd units keep working.
The new flag is optional. Document that `--local` does not rewrite the
file; the operator copies the example and restarts if a
restart-required key changed.

## 10. Required tests

- Parse and pass the local example under `--local`.
- Three negative fixtures, one per source class.
- Plain `check-config` still accepts the default example's model-info
  setting.
- Output snapshot or assertion that a dummy API key placed in the
  config is not present in the error string.

## 11. Required verification commands

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- --test-threads=1
```

Run the config-focused test target that already covers `check-config`
if `cli_contract` does not. Name it in the closure record.

## 12. Documentation updates

- `docs/configuration.md`
- `docs/cli-reference.md`
- `docs/deployment.md` with a short local-gateway note
- `rust/config.example.toml` only to point at the local example, not to
  change defaults

## 13. Acceptance criteria

`eggpool check-config --local` succeeds on the local example and fails
when any metadata or update source is enabled, naming the key. Plain
`check-config` behavior is unchanged. No network call is added.

## 14. Stop conditions

Stop if the check cannot see a source without executing a fetch. Stop
if making the profile work requires changing defaults for existing
configs. Stop if provider base URLs would be blocked; that is a
different policy. Do not add a firewall or a new HTTP client.

## 15. Closure evidence required

The example file path, pass/fail fixtures, proof the default path is
unchanged, doc links, and the serial command outcomes.

## 16. Handoff notes

Read `ModelInfoSourcesConfig::default` before writing the example so
the file overrides the OpenRouter default explicitly. Do not log the
loaded config struct; its debug output redacts keys, but the doctor
should not print the struct at all.
