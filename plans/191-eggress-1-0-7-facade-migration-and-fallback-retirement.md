# Plan 191: Eggress 1.0.7 facade migration and SSH fallback retirement

> **Status:** READY FOR IMPLEMENTATION
>
> **Baseline:** Eggpool `main` at `153aa54c5bdc7c7be07399f1ab833db9ef55f5f3`
>
> **Parent context:** completed Plans 186–190
>
> **Scope:** adopt published Eggress `1.0.7`, move production SSH proxy execution fully onto `eggress-embed::outbound::OutboundConnector`, retire Eggpool's temporary 1.0.6 SSH executor fallback and its direct production implementation-crate ownership, preserve the deterministic test-only proxy TLS-root seam, and re-qualify behavior/dependency/footprint boundaries.

## Executive summary

Eggpool is currently pinned to Eggress `1.0.6`. The production provider transport normally uses the stable `eggress-embed::outbound::OutboundConnector`, but SSH-containing pproxy expressions are diverted into a temporary Eggpool-owned executor because Eggress 1.0.6's embed facade did not install SSH session state. That workaround is exposed through the default `eggress-ssh-fallback` feature and directly activates `eggress-core`, `eggress-config`, `eggress-pproxy-compat`, `eggress-server`, `eggress-uri`, and `eggress-transport-ssh`.

Eggress `1.0.7` closes that facade gap. Its `OutboundConnector` owns SSH session state internally, keeps native/TOML verified-host behavior distinct from explicit pproxy compatibility behavior, and its `ssh` feature weak-forwards pproxy SSH support instead of forcing the pproxy compatibility crate into SSH-only builds. The Eggress regression suite exercises real SSH byte traversal, fail-closed/redacted authentication failure, and native untrusted-host rejection.

The correct Eggpool migration is therefore **not** to update the version while leaving the fallback in place. The intended result is:

```text
Production provider proxy path
  Eggpool
    -> eggress-embed 1.0.7 OutboundConnector
       -> direct / HTTP / SOCKS / SS / SSR / Trojan / SSH / multihop

Test-only custom proxy TLS trust path
  Eggpool test-support adapter
    -> narrowly scoped Eggress implementation crates
       only to inject an ephemeral verified TLS root for local fixtures
```

The custom-root path remains intentional because `OutboundConnector` still does not expose an arbitrary caller-supplied `rustls::ClientConfig`, and adding such a public security-sensitive Eggress API solely for Eggpool tests would increase maintenance surface for no production benefit.

This should be one bounded migration. Do not turn it into another Eggress abstraction redesign.

---

## Current state and evidence

### Current Cargo boundary

`rust/Cargo.toml` currently has:

```toml
[features]
default = ["eggress-ssh-fallback"]
test-support = ["eggress-ssh-fallback"]

eggress-ssh-fallback = [
    "eggress-embed/ssh",
    "dep:eggress-core",
    "dep:eggress-config",
    "dep:eggress-pproxy-compat",
    "dep:eggress-server",
    "dep:eggress-uri",
    "dep:eggress-transport-ssh",
]
```

The facade and every direct Eggress implementation dependency are pinned to `=1.0.6`.

The current no-default feature contract deliberately excludes SSH, while the default product includes it. Preserve that useful capability distinction, but rename it around product capability rather than an obsolete workaround.

### Current provider transport boundary

`rust/src/providers/transport.rs` currently:

1. uses `OutboundConnector` for non-SSH proxy expressions;
2. uses `proxy_uses_ssh()` to detect any SSH hop;
3. routes SSH to `build_ssh_proxy_dialer()`;
4. constructs an Eggpool-owned `ChainEgressProxyDialer` through `eggress-config`, `eggress-pproxy-compat`, `eggress-server`, `eggress-uri`, and `eggress-transport-ssh`;
5. creates `SshSessionCache::new_compatibility()` itself;
6. reuses that same low-level chain builder for the `test-support` custom TLS-root constructor.

That production exception is exactly what 1.0.7 makes obsolete.

### Existing test-only trust seam

`ProviderHttpClient::new_with_proxy_test_root` is gated by `test-support` and is used by the Trojan local fixture to provide a deterministic CA while retaining normal certificate and hostname verification. Plans 188–189 correctly concluded that this test seam should remain private/test-only rather than expanding Eggress's stable facade.

The live SSH tests use the normal production constructor rather than the custom-root constructor. Therefore the custom-root seam no longer needs to own SSH session state after this migration.

---

# Desired end state

After this plan lands:

1. All explicitly selected Eggress crates in Eggpool use exactly `1.0.7`.
2. Ordinary provider proxy construction, including SSH and SSH-containing multihop chains, uses only `eggress-embed::outbound::OutboundConnector`.
3. No production code parses or reconstructs Eggress chain internals for SSH.
4. `eggress-ssh-fallback` no longer exists as a feature or code path.
5. Eggpool retains a clean root `ssh` capability feature:
   - default build: SSH enabled;
   - `--no-default-features`: SSH disabled and SSH proxy configuration fails closed during construction;
   - `test-support`: implies `ssh` so the full provider transport qualification still exercises SSH.
6. Direct Eggress implementation dependencies remain only where the custom-root test adapter genuinely requires them.
7. `eggress-transport-ssh` is no longer a direct Eggpool dependency.
8. The custom-root adapter does not build or own an SSH session cache.
9. Credential redaction, proxy failure classification, cancellation/timeouts, no-direct-fallback behavior, and target routing remain unchanged.
10. The default release footprint is measured against the current qualified 1.0.6/fallback baseline; the result is recorded honestly even if the main win is maintenance ownership rather than size.

---

# Non-goals

Do **not** use this migration to:

- add an Eggpool-specific API or feature to Eggress;
- expose `ChainExecutor`, `SshSessionCache`, or arbitrary executor construction through `eggress-embed`;
- add a generic custom-certificate-verifier or insecure TLS hook to Eggress;
- weaken SSH host-key policy or TLS verification;
- remove supported pproxy protocols to reduce binary size;
- replace real proxy fixtures with mocks;
- change provider retry/failover/account routing behavior;
- replace Hyper, Rustls, Axum, or any unrelated transport component;
- revisit the broader Eggfetch integration line of work;
- add a feature-power-set CI matrix;
- chase transitive Eggress crates out of `Cargo.lock` when they remain legitimate facade dependencies;
- bump unrelated dependencies merely because a newer version exists.

---

# Workstream 0 — Reconfirm the published dependency surface

Before editing, confirm the registry resolves the release the user has published:

```bash
cargo search eggress-embed --limit 5
cargo info eggress-embed@1.0.7
```

Then create a minimal temporary Cargo project or use `cargo metadata` after the manifest edit to verify that `eggress-embed = "=1.0.7"` resolves from crates.io without a git/path override.

Do not use the Eggress repository `main` branch as a permanent dependency. The migration target is the published `1.0.7` crate line.

If crates.io propagation is incomplete for one of the exact internal Eggress packages, stop before committing partial mixed-version pins. Resume only when the complete 1.0.7 graph resolves.

---

# Workstream 1 — Upgrade the complete Eggress line to 1.0.7

## 1.1 Production facade

Update:

```toml
eggress-embed = { version = "=1.0.7", default-features = false, features = [
    "pproxy-compat",
    "pproxy-legacy",
    "legacy-crypto",
] }
```

Keep SSH activation on the Eggpool capability feature rather than embedding it unconditionally in the dependency declaration.

## 1.2 Replace the workaround feature with a capability feature

Preferred feature topology:

```toml
[features]
default = ["ssh"]
ssh = ["eggress-embed/ssh"]

test-support = [
    "ssh",
    "dep:eggress-core",
    "dep:eggress-config",
    "dep:eggress-pproxy-compat",
    "dep:eggress-server",
    "dep:eggress-uri",
]
```

This preserves the semantic behavior established by Plan 190:

- default builds support SSH;
- reduced/no-default builds do not;
- test-support runs the normal SSH product path plus the custom-root fixture path.

Do not keep `eggress-ssh-fallback` as a misleading permanent alias unless a real external build consumer is discovered that depends on that feature name. `eggpool` is currently `publish = false`, so internal repository feature cleanup is preferred over carrying an obsolete compatibility name indefinitely.

## 1.3 Minimize the test-support implementation dependency set

The custom-root adapter is currently the legitimate remaining reason for direct implementation crates. Start from the actual source requirements, not the old fallback list.

Expected optional `1.0.7` dependencies:

- `eggress-core` — target/stream/executor-facing types used by the private custom-root dialer;
- `eggress-config` — compile translated test proxy configuration;
- `eggress-pproxy-compat` — parse/translate the test pproxy URI;
- `eggress-server` — build the executor with the supplied test TLS config;
- `eggress-uri` — chain hop type stored by the private test dialer.

Expected removal:

- `eggress-transport-ssh` as a **direct** dependency.

For `eggress-server`, the test-root path currently needs Trojan support, not SSH. Prefer the narrowest verified feature set, expected to be:

```toml
eggress-server = {
    version = "=1.0.7",
    default-features = false,
    optional = true,
    features = ["extended"],
}
```

Do not retain `ssh`, `legacy-crypto`, or `pproxy-legacy` on the direct test-only server dependency unless compilation or a current custom-root test proves they are required. The ordinary SS/SSR/SSH provider tests use the facade path and should not dictate features on this private TLS-root adapter.

If the direct `eggress-pproxy-compat` test dependency needs a feature for the Trojan translation path, add only the demonstrated feature and document why.

## 1.4 Upgrade dev-only fixture crates

Update all directly selected Eggress fixture crates to `=1.0.7`, including at minimum:

- `eggress-core`;
- `eggress-protocol-shadowsocks`;
- `eggress-protocol-trojan`.

Retain their current fixture-specific feature requirements unless the existing tests prove a safe reduction.

## 1.5 Lockfile coherence

Regenerate `rust/Cargo.lock` through normal Cargo resolution. Verify there is no mixed explicit Eggress `1.0.6`/`1.0.7` line:

```bash
cargo tree --manifest-path rust/Cargo.toml | rg 'eggress-[A-Za-z0-9_-]+ v1\.0\.(6|7)'
```

Every Eggress package selected for this graph should resolve to `1.0.7` unless an independently versioned package proves otherwise. Do not use `[patch]`, git dependencies, or path overrides to force coherence.

---

# Workstream 2 — Remove the production SSH fallback implementation

Primary file: `rust/src/providers/transport.rs`.

## 2.1 Route every production proxy through OutboundConnector

Simplify `ProviderTcpConnector::new` to the stable facade boundary:

```text
None                 -> direct HttpConnector
Some("direct://")    -> validate through OutboundConnector, then intentional direct connector
Some(other proxy)    -> EgressProxyDialer(OutboundConnector::from_pproxy_uri(...))
```

SSH must no longer have a separate production match arm.

Delete production-only workaround pieces that become dead:

- `proxy_uses_ssh()` if no longer needed for any capability/error contract;
- `build_ssh_proxy_dialer()` enabled and disabled variants;
- production `ChainEgressProxyDialer` use;
- direct production imports of `TargetAddr`, `TargetHost`, `ChainExecutor`, `ProxyHopSpec`, etc.;
- creation of `SshSessionCache::new_compatibility()`;
- comments explaining the Eggress 1.0.6 facade gap.

The live SSH path must now be the same `EgressProxyDialer` type used by other Eggress proxy chains.

## 2.2 Preserve reduced-feature failure semantics

With `--no-default-features`, `eggress-embed/ssh` is absent. An SSH URI passed to `OutboundConnector::from_pproxy_uri` must fail closed as an unsupported/invalid proxy configuration; Eggpool should map that construction failure to the existing `TransportError::ProxyConfiguration`.

Prefer relying on Eggress's feature-aware parser/compiler rather than preserving Eggpool's hand-written `proxy_uses_ssh()` classifier solely to reject SSH. This removes duplicated protocol knowledge.

If 1.0.7 unexpectedly accepts an SSH URI without the `ssh` feature but only fails later during dialing, keep the smallest explicit construction-time capability guard needed to preserve Plan 190's fail-before-network contract, and document that behavior. Do not reintroduce the native executor fallback.

## 2.3 Preserve direct:// semantics

Keep the current explicit `direct://` control behavior:

1. validate the expression through Eggress;
2. intentionally use Eggpool's direct `HttpConnector`;
3. keep it distinguishable from proxy failure;
4. never use direct transport as recovery from a failed proxy.

---

# Workstream 3 — Retain only the test-only custom TLS-root adapter

The custom-root constructor remains legitimate, but its implementation must no longer be conflated with the removed SSH workaround.

## 3.1 Gate the low-level chain dialer only on test-support

Rename the remaining low-level types/helpers to make their scope obvious, for example:

- `ChainEgressProxyDialer` -> `TestRootProxyDialer`;
- `build_chain_egress_dialer` -> `build_test_root_proxy_dialer`.

Gate the entire low-level implementation with:

```rust
#[cfg(feature = "test-support")]
```

not with the deleted fallback feature.

Ordinary production source outside that block should import only `eggress_embed` from the Eggress family.

## 3.2 Remove SSH ownership from the test adapter

The custom-root path exists to inject deterministic proxy TLS trust. It should not create an SSH session cache.

Construct the 1.0.7 server executor with the supplied TLS config and no SSH facility using the feature-appropriate `eggress-server` API. Conceptually:

```rust
let executor = eggress_server::build_chain_executor(Some(tls_config), None);
```

Use the exact 1.0.7 signature that compiles with `eggress-server`'s non-SSH feature slice.

The test-root constructor does not need to promise SSH support. Current SSH qualification uses `new_with_proxy`, which is the production facade path.

## 3.3 Preserve strict trust behavior

The following Plan 188 invariants remain mandatory:

- supplied test CA succeeds for the real local Trojan TLS proxy;
- missing/untrusted CA fails verification;
- hostname verification remains enabled;
- no `danger_accept_invalid_certs`, no no-op verifier, no blanket insecure mode;
- production constructors cannot consume the custom root;
- credential-bearing proxy URLs remain absent from public/debug errors.

Do not request a new Eggress facade trust hook solely to delete these few optional test dependencies.

---

# Workstream 4 — Update and strengthen migration regressions

Primary suite: `rust/tests/provider_transport.rs`.

## 4.1 Prove SSH now works through the normal facade

Keep the existing live OpenSSH fixture and normal `ProviderHttpClient::new_with_proxy` SSH success test. Its success after the fallback code is deleted is the key migration proof.

Retain/verify:

- actual byte traversal to the provider target;
- private-key authentication behavior;
- authentication failure classification;
- authentication error redaction;
- connect timeout/cancellation bounds;
- no proxy-to-direct fallback;
- SSH in multihop chains if already covered by the current corpus.

Do not replace this with a construction-only test.

## 4.2 Rename the no-default SSH regression around capability, not fallback

The current test:

```text
ssh_proxy_is_rejected_when_the_compatibility_fallback_is_disabled
```

becomes obsolete because there is no fallback.

If the root `ssh` capability feature is retained as specified, replace it with a `#[cfg(not(feature = "ssh"))]` regression asserting that an SSH proxy is rejected as `TransportError::ProxyConfiguration` before network activity.

Rename it accordingly, e.g.:

```text
ssh_proxy_is_rejected_when_ssh_capability_is_disabled
```

Also keep at least one non-SSH proxy construction/behavior case active under no-default features so a blanket proxy rejection cannot satisfy the reduced-feature test.

## 4.3 Preserve the custom-root pair

Retain the verified Trojan tests under `test-support`:

- success with the supplied test CA;
- failure without that CA / with untrusted trust state;
- auth failure redaction and no direct fallback.

These tests justify the remaining optional implementation-crate seam.

## 4.4 Preserve full pproxy transport corpus

The migration must not regress:

- HTTP CONNECT;
- SOCKS4/5 and authenticated variants;
- Shadowsocks;
- SSR/legacy compatibility;
- Trojan;
- SSH;
- ordered multihop chains;
- explicit `direct://`;
- credential redaction;
- provider/account client isolation;
- pool/connect/read/write timeout behavior.

Any protocol regression should be treated as a migration blocker, not worked around by restoring the old fallback wholesale.

---

# Workstream 5 — Dependency ownership and feature-graph qualification

## 5.1 Source ownership search

Run:

```bash
rg 'eggress_(core|config|pproxy_compat|server|uri|transport_ssh)' rust/src
```

Expected result:

- no `eggress_transport_ssh` references;
- any remaining implementation-crate references exist only inside the explicit `#[cfg(feature = "test-support")]` custom-root block;
- ordinary provider construction imports only `eggress_embed`.

Also run:

```bash
rg 'eggress-ssh-fallback|Eggress 1\.0\.6|1\.0\.6.*Eggress' rust .github architecture docs plans README.md
```

Do not rewrite completed historical plans wholesale. Add concise supersession/follow-up notes where necessary; source/docs/current architecture must not describe the fallback as active after migration.

## 5.2 Feature trees

Capture:

```bash
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml -e features --no-default-features
cargo tree --manifest-path rust/Cargo.toml -e features --features test-support
```

Confirm:

### Default

- root `ssh` is active;
- `eggress-embed/ssh` is active;
- there is no root direct `eggpool -> eggress-transport-ssh` edge;
- any transport-ssh crate in the graph is owned transitively by the facade.

### No-default

- root `ssh` is absent;
- `eggress-embed/ssh` is absent;
- SSH configuration fails closed;
- non-SSH facade behavior remains available.

### test-support

- `ssh` is active for normal SSH qualification;
- the custom-root direct implementation dependencies are active;
- direct `eggress-transport-ssh` remains absent;
- direct `eggress-server` does not activate SSH merely for the TLS-root adapter.

Use inverse queries to distinguish direct ownership from facade transitives:

```bash
cargo tree --manifest-path rust/Cargo.toml -i eggress-embed
cargo tree --manifest-path rust/Cargo.toml -i eggress-server
cargo tree --manifest-path rust/Cargo.toml -i eggress-transport-ssh
```

Do not call transitive facade-owned crates a failed cleanup.

---

# Workstream 6 — CI adjustment

The existing CI already compiles and lints `--no-default-features`, which is valuable and should remain.

Update only what feature renaming/removal requires. Do not add a matrix or a second general Rust job.

Because the actual SSH fixture requires `sshd`, it is acceptable for the full live SSH transport qualification to remain a local/release closure command if the current hosted CI does not provision OpenSSH. Do not install a new service into ordinary Eggpool CI solely because Eggress's own CI already qualifies its internal facade.

However, the Eggpool implementation commit must locally run the live provider SSH suite before closure, because the purpose of this migration is to prove Eggpool now traverses that fixed facade correctly.

If CI currently runs tests that were conditionally keyed to `eggress-ssh-fallback`, update those cfgs to the new `ssh` capability name.

---

# Workstream 7 — Footprint and maintenance closure

The previous comparable baseline recorded by Plan 189 is:

```text
release binary:       29,570,824 bytes
cargo-bloat .text:    approximately 17.9 MiB
```

After 1.0.7 migration, perform a same-host/toolchain/profile release build:

```bash
cargo build --manifest-path rust/Cargo.toml --release --locked
ls -l rust/target/release/eggpool
```

If `cargo-bloat` is already available:

```bash
cargo bloat --manifest-path rust/Cargo.toml --release --crates
```

Also record the direct-dependency and feature-tree change.

Interpretation rules:

- a smaller binary is useful but not required;
- roughly unchanged size is acceptable because the primary win is removal of Eggpool-owned executor/session-cache code and direct production dependencies;
- a material increase should be investigated for accidental default-feature expansion or duplicate 1.0.6/1.0.7 resolution before closure.

Do not change release LTO/strip/codegen settings to manufacture a favorable comparison.

---

# Workstream 8 — Documentation and plan lifecycle cleanup

Update current source-of-truth documentation that describes the 1.0.6 exception. Likely targets include:

- `architecture/deep-dive-providers.md`;
- provider/transport comments;
- any current dependency/architecture documentation that calls `eggress-ssh-fallback` active.

Desired durable wording:

```text
Eggpool delegates provider proxy-chain construction and execution to
`eggress-embed` 1.0.7. SSH is an optional Eggpool capability enabled by
default; the facade owns SSH session state. Eggpool retains one private
`test-support` adapter only for deterministic custom proxy TLS roots.
```

Add concise follow-up/supersession notes to completed Plans 186–190 only where useful to prevent a future agent from treating the 1.0.6 exception as current. Do not rewrite their historical closure evidence; it accurately describes what was true at the time.

At minimum, Plan 190 should receive a note that its `eggress-ssh-fallback` capability boundary was retired by Plan 191 after the upstream fix shipped in 1.0.7.

Append closure evidence to this Plan 191 containing:

- implementation commit;
- exact Eggress version line;
- final root feature topology;
- final direct Eggress dependency classification;
- provider transport test count/result;
- no-default result;
- all-features result;
- live SSH success/auth-failure/timeout results;
- test-root Trojan success/failure results;
- dependency tree/inverse-query findings;
- release binary size and optional cargo-bloat result;
- `cargo deny` result;
- confirmation that no Eggpool-specific Eggress API was added.

---

# Mandatory verification

Run from repository root unless noted otherwise.

## Formatting and compilation

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --locked
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --locked --no-default-features
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --locked --features test-support
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --locked --all-features
```

## Clippy

```bash
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --locked -- -D warnings
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --locked --no-default-features -- -D warnings
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --locked --all-features -- -D warnings
```

## Behavioral qualification

```bash
cargo test --manifest-path rust/Cargo.toml --locked --no-default-features -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --locked --features test-support --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --locked --all-features -- --test-threads=1
```

The provider transport invocation must run on a host with `sshd`/`ssh-keygen` available so the live SSH fixture actually executes.

If the fixture currently hard-fails when OpenSSH is unavailable, retain that behavior for the explicit local qualification command. Do not silently convert a required migration proof into a skip.

## Dependency/policy checks

```bash
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml -e features --no-default-features
cargo tree --manifest-path rust/Cargo.toml -e features --features test-support
cargo tree --manifest-path rust/Cargo.toml -i eggress-embed
cargo tree --manifest-path rust/Cargo.toml -i eggress-server
cargo tree --manifest-path rust/Cargo.toml -i eggress-transport-ssh
cargo deny --manifest-path rust/Cargo.toml check
git diff --check
```

## Release measurement

```bash
cargo build --manifest-path rust/Cargo.toml --release --locked
ls -l rust/target/release/eggpool
```

Optional only if already installed:

```bash
cargo bloat --manifest-path rust/Cargo.toml --release --crates
```

---

# Expected files to change

Primary:

- `rust/Cargo.toml`
- `rust/Cargo.lock`
- `rust/src/providers/transport.rs`
- `rust/tests/provider_transport.rs`

Likely current-documentation/closure updates:

- `architecture/deep-dive-providers.md`
- `plans/190-eggress-optional-feature-corrective-closure.md`
- this plan's closure section

Possible CI edit only if feature-name references require it:

- `.github/workflows/ci.yml`

Do not modify unrelated provider adapters, routing state, retry state, database migrations, updater behavior, dashboard code, or Python tooling.

---

# Implementation order

1. Confirm complete Eggress 1.0.7 registry resolution.
2. Change all direct/dev Eggress pins to 1.0.7 and resolve the lockfile.
3. Introduce the root `ssh` capability feature and split `test-support` from the old fallback dependency set.
4. Remove the SSH-specific production branch and route normal SSH through `OutboundConnector`.
5. Rename/isolate the remaining low-level custom-root adapter under `test-support`.
6. Remove the direct `eggress-transport-ssh` dependency and SSH cache construction.
7. Minimize the test-only `eggress-server`/compat feature set based on actual compile/test evidence.
8. Update cfg-gated tests from fallback terminology to SSH capability terminology.
9. Run the live provider transport suite with OpenSSH available.
10. Run no-default, default, test-support, and all-features compile/lint/test qualification.
11. Inspect dependency/feature trees and verify no explicit 1.0.6 remnants or direct production implementation ownership.
12. Run policy checks and comparable release measurement.
13. Update current architecture/docs and append historical-plan follow-up notes.
14. Push the implementation commit and require normal CI green.
15. Append final closure evidence and mark this plan complete only after the pushed head is verified.

---

# Failure and rollback rules

If 1.0.7 introduces a regression in an existing non-SSH proxy protocol, first determine whether it is a feature-resolution mistake in Eggpool or a real Eggress regression. Do not restore the entire 1.0.6 fallback architecture as a generic workaround.

If the 1.0.7 `OutboundConnector` SSH path fails Eggpool's real OpenSSH fixture despite passing Eggress's own facade tests:

1. capture the exact URI and failure class without secrets;
2. compare the Eggpool URI/target usage with the Eggress regression fixture;
3. determine whether Eggpool depends on a compatibility behavior not covered upstream;
4. create a narrow Eggress corrective issue/plan only if an actual upstream defect is proven;
5. keep Eggpool on the last known-good state until the fixed published Eggress patch exists.

Do not ship a mixed 1.0.7 facade + copied native fallback merely to force closure unless a separately documented emergency decision explicitly accepts that temporary state.

---

# Completion criteria

Plan 191 is complete only when all applicable statements are true:

1. Eggpool resolves the published Eggress `1.0.7` line with no git/path overrides.
2. Every direct/dev Eggress pin selected by Eggpool is on 1.0.7.
3. `eggress-ssh-fallback` is removed from Cargo features and source cfgs.
4. The default product retains SSH through a normal `ssh -> eggress-embed/ssh` capability feature.
5. `ProviderTcpConnector::new` routes SSH through `OutboundConnector`, not an Eggpool-built executor.
6. Production source no longer creates `SshSessionCache` or reconstructs Eggress SSH chain state.
7. `eggress-transport-ssh` is not a direct Eggpool dependency.
8. Direct implementation-crate source usage is confined to the `test-support` custom-root adapter.
9. The custom-root adapter owns no SSH state and retains strict TLS verification.
10. Live SSH traversal passes through the normal constructor.
11. SSH authentication failure remains correctly classified and credential-redacted.
12. SSH timeout/cancellation remains bounded.
13. Proxy failures cannot fall back to direct egress.
14. No-default builds compile/lint/test and reject SSH capability fail-closed while preserving non-SSH proxy behavior.
15. Trojan custom-root success and untrusted-root failure both remain green.
16. HTTP/SOCKS/SS/SSR/Trojan/multihop/direct behavior remains qualified.
17. Dependency-tree evidence confirms facade ownership rather than direct production implementation ownership.
18. No unexpected Eggress 1.0.6 package remains in the selected graph.
19. `cargo deny` passes.
20. The release footprint delta versus the Plan 189 baseline is recorded and any material regression explained.
21. Current documentation no longer describes the 1.0.6 fallback as active.
22. Plan 190 is annotated as historically complete but superseded for current SSH architecture by this 1.0.7 migration.
23. No new Eggpool-specific Eggress API or insecure trust escape hatch was introduced.
24. This plan contains pushed-head closure evidence and is marked complete only after CI/qualification verification.

---

# Handoff note

The goal is not "use a newer dependency." The goal is to consume the upstream ownership correction that Eggpool originally needed, delete the local compatibility ownership that correction makes obsolete, and leave a smaller, clearer trust/dependency boundary.

Once the facade path passes Eggpool's real SSH fixture and the custom-root seam is cleanly test-only, stop. Further Eggress integration or general proxy refactoring belongs in a separate plan with independent justification.
