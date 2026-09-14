# Plan 191: Eggress 1.0.7 facade migration and SSH fallback retirement

> **Status:** READY FOR IMPLEMENTATION
>
> **Baseline:** Eggpool `main` immediately before this plan at `9144ff910f2ef62055e0560fab850ad224f36c6d`
>
> **Parent context:** completed Plans 186–190
>
> **Scope:** adopt published Eggress `1.0.7`, move production SSH proxy execution fully onto `eggress-embed::outbound::OutboundConnector`, retire Eggpool's temporary 1.0.6 SSH executor fallback and its direct production implementation-crate ownership, preserve the deterministic test-only proxy TLS-root seam, and re-qualify behavior/dependency/footprint boundaries.

## Executive summary

Eggpool is pinned to Eggress `1.0.6`. Normal provider proxy construction uses the stable `eggress-embed::outbound::OutboundConnector`, but SSH-containing pproxy expressions are diverted into an Eggpool-owned executor because Eggress 1.0.6's embed facade did not install SSH session state. That workaround is exposed through the default `eggress-ssh-fallback` feature and directly activates `eggress-core`, `eggress-config`, `eggress-pproxy-compat`, `eggress-server`, `eggress-uri`, and `eggress-transport-ssh`.

Eggress `1.0.7` closes that gap. Its `OutboundConnector` owns SSH session state internally; native/TOML SSH retains verified host-key policy while explicit pproxy compatibility uses the compatibility policy; the `ssh` feature weak-forwards pproxy SSH support rather than requiring the pproxy compatibility crate in an SSH-only consumer. Eggress's closure suite exercises real SSH byte traversal, fail-closed/redacted authentication failure, and native rejection of an untrusted host key.

The correct migration is therefore not merely a version bump. The intended ownership boundary is:

```text
Production provider proxy path
  Eggpool
    -> eggress-embed 1.0.7 OutboundConnector
       -> HTTP / SOCKS / SS / SSR / Trojan / SSH / multihop

Test-only custom proxy TLS trust path
  Eggpool test-support adapter
    -> narrowly scoped Eggress implementation crates
       only to inject an ephemeral verified TLS root for local fixtures
```

The custom-root path remains intentional because `OutboundConnector` does not expose arbitrary caller-supplied `rustls::ClientConfig`, and adding a security-sensitive public Eggress hook solely to simplify Eggpool tests would increase long-term API burden without a production use case.

The commit `9144ff9` that landed during research changed only Plan 190 closure evidence; it did not alter the Cargo/source integration described below.

---

## Current state

### Cargo boundary

`rust/Cargo.toml` currently declares:

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

`eggress-embed`, all optional implementation crates, and the Eggress fixture crates are pinned to `=1.0.6`.

Plan 190 also established a useful product property: the default build supports SSH, while `--no-default-features` is a supported reduced configuration that rejects SSH fail-closed. Preserve that capability distinction; only the fallback implementation is obsolete.

### Provider transport boundary

`rust/src/providers/transport.rs` currently:

1. uses `OutboundConnector` for non-SSH proxy expressions;
2. detects SSH with `proxy_uses_ssh()`;
3. sends SSH to `build_ssh_proxy_dialer()`;
4. reconstructs a chain using config/pproxy/server/uri implementation crates;
5. creates `SshSessionCache::new_compatibility()` itself;
6. reuses the same low-level chain dialer for the `test-support` custom TLS-root constructor.

The first five points are exactly the temporary ownership that 1.0.7 removes.

### Test-only trust seam

`ProviderHttpClient::new_with_proxy_test_root` is `test-support` only and is used by the real Trojan fixture to install a deterministic CA while retaining certificate and hostname verification. The live SSH fixture uses ordinary `ProviderHttpClient::new_with_proxy`, not the custom-root constructor. Therefore, after the 1.0.7 migration, the custom-root seam no longer needs SSH session-cache ownership.

---

# Desired end state

1. All explicitly selected Eggress crates use `1.0.7`.
2. Normal provider proxy construction, including SSH and SSH-containing multihop chains, uses only `eggress-embed::outbound::OutboundConnector`.
3. Eggpool production code no longer reconstructs Eggress chain/config/executor state for SSH.
4. `eggress-ssh-fallback` is removed as a feature and source path.
5. A root `ssh` capability feature preserves existing semantics:
   - default build: SSH enabled;
   - `--no-default-features`: SSH disabled and rejected during connector construction;
   - `test-support`: implies `ssh` so the full transport suite exercises the normal production SSH path.
6. Direct implementation dependencies remain only for the deterministic custom-root test adapter where demonstrably required.
7. `eggress-transport-ssh` is not a direct Eggpool dependency.
8. The custom-root adapter creates no SSH cache/state.
9. Proxy failure classification, credential redaction, timeouts/cancellation, multihop order, and no-direct-fallback behavior remain unchanged.
10. A comparable release measurement records any footprint change without making size reduction a prerequisite for the maintenance-boundary win.

---

# Non-goals

Do not use this pass to:

- add an Eggpool-specific Eggress API/feature/type;
- expose Eggress `ChainExecutor` or `SshSessionCache` through the stable facade;
- add an insecure/general-purpose TLS verifier hook;
- weaken SSH host-key or proxy TLS verification;
- remove supported proxy protocols for size;
- replace live proxy fixtures with mocks;
- change provider routing/retry/account behavior;
- replace Hyper/Rustls/Axum or reopen Eggfetch work;
- add a feature-power-set CI matrix;
- eliminate implementation crates that legitimately remain transitive through `eggress-embed`;
- update unrelated dependencies merely because newer releases exist.

---

# Workstream 0 — Verify registry resolution

Confirm the published release resolves from crates.io before editing the dependency graph:

```bash
cargo search eggress-embed --limit 5
cargo info eggress-embed@1.0.7
```

After editing the manifest, require `cargo metadata --locked`/normal Cargo resolution to use registry packages only. Do not use a git/path override as the long-term migration.

If one of Eggress's exact internal 1.0.7 packages has not propagated yet, do not commit a mixed 1.0.6/1.0.7 state. Resume only after the complete line resolves.

---

# Workstream 1 — Upgrade and rewire Cargo features

## 1.1 Facade

Use:

```toml
eggress-embed = { version = "=1.0.7", default-features = false, features = [
    "pproxy-compat",
    "pproxy-legacy",
    "legacy-crypto",
] }
```

Keep SSH controlled by the root product feature rather than enabling it unconditionally here.

## 1.2 Replace fallback with capability

Preferred topology:

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

Because the crate is `publish = false`, remove `eggress-ssh-fallback` rather than preserving an obsolete compatibility alias unless a real external build consumer is discovered during implementation.

## 1.3 Minimize test-support implementation dependencies

Expected optional `=1.0.7` dependencies are:

- `eggress-core` — target/executor-facing types used by the private custom-root dialer;
- `eggress-config` — compile translated fixture configuration;
- `eggress-pproxy-compat` — parse/translate the fixture pproxy URI;
- `eggress-server` — create the executor with the supplied test TLS configuration;
- `eggress-uri` — chain-hop type stored by the private test dialer.

Expected direct removal:

- `eggress-transport-ssh`.

For the direct test-only `eggress-server`, start with the minimum feature set required by the current custom-root Trojan fixture, expected to be:

```toml
eggress-server = {
    version = "=1.0.7",
    default-features = false,
    optional = true,
    features = ["extended"],
}
```

Do not retain `ssh`, `legacy-crypto`, or `pproxy-legacy` on this direct test dependency unless a current custom-root test proves it needs them. SS/SSR/SSH qualification uses the normal facade path and should not dictate features on the custom-root adapter.

## 1.4 Dev fixtures

Update all directly selected Eggress dev dependencies to `=1.0.7`, including at minimum:

- `eggress-core`;
- `eggress-protocol-shadowsocks`;
- `eggress-protocol-trojan`.

Keep fixture-specific legacy features only where the current suite needs them.

## 1.5 Lockfile and single-line proof

Regenerate `rust/Cargo.lock` normally. Verify no selected 1.0.6 line remains:

```bash
cargo tree --manifest-path rust/Cargo.toml | rg 'eggress-[A-Za-z0-9_-]+ v1\.0\.(6|7)'
```

Do not add `[patch]`, git dependencies, or path overrides to force resolution.

---

# Workstream 2 — Retire the production fallback

Primary file: `rust/src/providers/transport.rs`.

## 2.1 One production facade path

Simplify `ProviderTcpConnector::new` to:

```text
None              -> direct HttpConnector
Some("direct://") -> validate through OutboundConnector, then intentional direct connector
Some(other)       -> EgressProxyDialer(OutboundConnector::from_pproxy_uri(...))
```

SSH must no longer have a distinct production match arm.

Remove production workaround code that becomes dead:

- `proxy_uses_ssh()` if no longer required for reduced-feature construction semantics;
- both `build_ssh_proxy_dialer()` cfg variants;
- production `ChainEgressProxyDialer` ownership;
- production imports of `TargetAddr`, `TargetHost`, `ProxyHopSpec`, executor internals, etc.;
- `SshSessionCache::new_compatibility()` construction;
- comments describing the 1.0.6 facade gap as current.

The existing `EgressProxyDialer` must handle SSH exactly as it handles other Eggress chains.

## 2.2 Preserve no-default failure semantics

Without the root `ssh` feature, `eggress-embed/ssh` is absent. Prefer relying on 1.0.7's feature-aware parser/compiler so `OutboundConnector::from_pproxy_uri("ssh://...")` fails construction and Eggpool maps that to `TransportError::ProxyConfiguration`.

Do not retain Eggpool's hand-written SSH classifier merely out of habit; deleting duplicated protocol knowledge is part of the maintenance win.

If 1.0.7 unexpectedly accepts SSH without the feature and fails only during dialing, keep only the smallest explicit construction-time capability check needed to preserve Plan 190's fail-before-network contract. Never restore the native executor fallback.

## 2.3 Keep direct:// intentional

Preserve the existing explicit `direct://` semantics: validate the expression through Eggress, then intentionally use Eggpool's direct connector. Proxy failures must never select this path as fallback.

---

# Workstream 3 — Make the low-level path test-root-only

## 3.1 Rename and gate it by purpose

Retain the low-level executor only for `new_with_proxy_test_root` and gate all of it with `#[cfg(feature = "test-support")]`.

Prefer names that expose intent, e.g.:

- `ChainEgressProxyDialer` -> `TestRootProxyDialer`;
- `build_chain_egress_dialer` -> `build_test_root_proxy_dialer`.

Outside that block, ordinary provider source should reference only `eggress_embed` from the Eggress family.

## 3.2 Remove SSH state from the test adapter

The test-root path exists to inject verified proxy TLS trust, not to implement SSH. Build the 1.0.7 server executor with the supplied TLS config and without an SSH session cache using the non-SSH signature selected by the final feature graph.

Conceptually:

```rust
let executor = eggress_server::build_chain_executor(Some(tls_config), None);
```

Use the exact 1.0.7 signature that compiles with the chosen test-only server features.

The custom-root constructor does not need to promise SSH support; the SSH fixture already uses `new_with_proxy`.

## 3.3 Preserve strict trust

Plan 188's security invariants remain mandatory:

- supplied fixture CA succeeds through real TLS verification;
- absent/untrusted CA fails;
- hostname verification remains enabled;
- production constructors cannot consume the test root;
- no insecure/no-op verifier is introduced;
- credential-bearing proxy URLs are not leaked through public/debug errors.

Do not request a new Eggress public trust hook solely to remove these test-only optional dependencies.

---

# Workstream 4 — Regression updates

Primary suite: `rust/tests/provider_transport.rs`.

## 4.1 Prove the fixed facade, not just construction

Keep the existing live OpenSSH fixture and ordinary `ProviderHttpClient::new_with_proxy` success test. After deleting fallback code, this is the key proof that Eggpool actually traverses `OutboundConnector` 1.0.7.

Retain assertions for:

- byte traversal to the provider target;
- private-key/auth behavior;
- auth failure classification;
- credential redaction;
- timeout/cancellation bounds;
- no proxy-to-direct fallback;
- SSH-containing multihop cases already in the corpus.

Do not replace this with a construction-only test.

## 4.2 Rename reduced-feature regression

Replace the now-obsolete `ssh_proxy_is_rejected_when_the_compatibility_fallback_is_disabled` test with a capability-oriented regression gated by `#[cfg(not(feature = "ssh"))]`, e.g.:

```text
ssh_proxy_is_rejected_when_ssh_capability_is_disabled
```

It must assert `TransportError::ProxyConfiguration` before network activity.

Keep at least one non-SSH proxy case active under no-default features so an implementation that rejects all proxies cannot satisfy the reduced-feature suite.

## 4.3 Preserve custom-root pair and full corpus

Retain the test-support Trojan success/failure pair and auth-redaction/no-direct-fallback behavior.

The migration must continue to qualify HTTP CONNECT, SOCKS4/5, Shadowsocks, SSR, Trojan, SSH, ordered multihop, explicit `direct://`, credential redaction, provider/account client isolation, and pool/connect/read/write timeout behavior.

---

# Workstream 5 — Ownership and feature-tree proof

## 5.1 Source search

Run:

```bash
rg 'eggress_(core|config|pproxy_compat|server|uri|transport_ssh)' rust/src
```

Expected:

- zero `eggress_transport_ssh` references;
- implementation-crate references only under `#[cfg(feature = "test-support")]`;
- ordinary provider construction uses only `eggress_embed`.

Also search current documentation/source for stale workaround wording:

```bash
rg 'eggress-ssh-fallback|Eggress 1\.0\.6|1\.0\.6.*Eggress' rust .github architecture docs README.md
```

Historical plans should receive concise supersession notes rather than rewritten historical bodies.

## 5.2 Feature trees

Capture:

```bash
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml -e features --no-default-features
cargo tree --manifest-path rust/Cargo.toml -e features --features test-support
```

Confirm:

**Default:** root `ssh` and `eggress-embed/ssh` are active; there is no direct `eggpool -> eggress-transport-ssh` edge.

**No-default:** root/embed SSH are absent; SSH configuration fails closed; non-SSH proxy functionality remains available.

**Test-support:** normal facade SSH is active; custom-root implementation deps are active; direct `eggress-transport-ssh` remains absent; direct `eggress-server` does not activate SSH solely for the TLS-root adapter.

Use inverse queries to distinguish transitive facade ownership from direct ownership:

```bash
cargo tree --manifest-path rust/Cargo.toml -i eggress-embed
cargo tree --manifest-path rust/Cargo.toml -i eggress-server
cargo tree --manifest-path rust/Cargo.toml -i eggress-transport-ssh
```

Transitive implementation crates through `eggress-embed` are expected and are not a cleanup failure.

---

# Workstream 6 — CI

The existing single Rust CI job already checks/clippies `--no-default-features`. Keep those gates.

Update CI only if cfg/feature-name changes require it; do not add a feature matrix or second general Rust job.

The implementation closure must run the live provider SSH suite on a host with `sshd`/`ssh-keygen`, because this migration specifically changes Eggpool's SSH execution path. It is acceptable for that environment-sensitive proof to remain an explicit local/release closure command rather than provisioning OpenSSH in every ordinary Eggpool CI run; Eggress already permanently qualifies its internal facade in its own CI.

---

# Workstream 7 — Footprint and maintenance closure

Plan 189's comparable baseline is:

```text
release binary:    29,570,824 bytes
cargo-bloat .text: ~17.9 MiB
```

After migration, perform the same-profile release build:

```bash
cargo build --manifest-path rust/Cargo.toml --release --locked
ls -l rust/target/release/eggpool
```

If `cargo-bloat` is already installed:

```bash
cargo bloat --manifest-path rust/Cargo.toml --release --crates
```

Record direct-dependency and feature-tree changes too.

Interpretation:

- smaller is useful but not required;
- roughly unchanged is acceptable because the primary improvement is ownership/maintenance simplification;
- a material increase must be investigated for accidental feature expansion or mixed Eggress resolution.

Do not change release LTO/strip/codegen settings to manufacture a size result.

---

# Workstream 8 — Documentation and plan lifecycle

Update current source-of-truth material that describes the 1.0.6 exception, likely including `architecture/deep-dive-providers.md` and provider source comments.

Durable target wording:

```text
Eggpool delegates provider proxy-chain construction and execution to
`eggress-embed` 1.0.7. SSH is an optional Eggpool capability enabled by
default; the facade owns SSH session state. Eggpool retains one private
`test-support` adapter only for deterministic custom proxy TLS roots.
```

Do not rewrite Plans 186–190: their closure evidence describes the architecture that was actually qualified at the time. Add concise follow-up notes where needed. At minimum Plan 190 should note that its `eggress-ssh-fallback` boundary was retired by Plan 191 after the upstream 1.0.7 fix shipped.

Append final closure evidence to this plan with:

- implementation/closure commit(s);
- exact Eggress line;
- final root feature topology;
- direct Eggress dependency classification;
- provider transport/no-default/all-features results;
- live SSH success/auth-failure/timeout results;
- test-root Trojan success/failure results;
- dependency-tree findings;
- release binary size and optional cargo-bloat result;
- `cargo deny` result;
- confirmation that no Eggpool-specific Eggress API was added.

---

# Mandatory verification

## Compile/lint

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --locked
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --locked --no-default-features
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --locked --features test-support
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --locked --all-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --locked -- -D warnings
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --locked --no-default-features -- -D warnings
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --locked --all-features -- -D warnings
```

## Behavior

```bash
cargo test --manifest-path rust/Cargo.toml --locked --no-default-features -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --locked --features test-support --test provider_transport -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --locked --all-features -- --test-threads=1
```

The provider transport invocation must be run where OpenSSH is available so the live SSH fixture actually executes.

## Dependency/policy

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

Optional if already installed:

```bash
cargo bloat --manifest-path rust/Cargo.toml --release --crates
```

---

# Expected files

Primary:

- `rust/Cargo.toml`
- `rust/Cargo.lock`
- `rust/src/providers/transport.rs`
- `rust/tests/provider_transport.rs`

Likely closure/docs:

- `architecture/deep-dive-providers.md`
- `plans/190-eggress-optional-feature-corrective-closure.md`
- this plan

CI only if feature-name references require it:

- `.github/workflows/ci.yml`

Do not modify unrelated routing, retries, DB, updater, dashboard/server, provider adapters, or Python tooling.

---

# Implementation order

1. Confirm complete 1.0.7 registry resolution.
2. Move every direct/dev Eggress pin to 1.0.7 and regenerate the lockfile.
3. Replace `eggress-ssh-fallback` with the root `ssh` capability and split `test-support` dependency ownership.
4. Route production SSH through `OutboundConnector` and delete fallback construction/state.
5. Rename/isolate the low-level custom-root adapter under `test-support`.
6. Remove direct `eggress-transport-ssh` and SSH cache construction.
7. Minimize direct test-only server/compat features using compile/test evidence.
8. Update cfg-gated tests to capability terminology.
9. Run the live provider transport suite with OpenSSH available.
10. Run no-default/default/test-support/all-features compile, lint, and tests.
11. Inspect feature/inverse trees and verify no 1.0.6 residue or direct production implementation ownership.
12. Run policy checks and comparable release measurement.
13. Update current docs and append historical follow-up notes.
14. Push implementation and require normal CI green.
15. Append pushed-head closure evidence and mark complete only after verification.

---

# Failure policy

If a non-SSH proxy protocol regresses on 1.0.7, first distinguish an Eggpool feature-resolution error from a real Eggress regression. Do not restore the whole 1.0.6 fallback architecture as a general workaround.

If the 1.0.7 facade SSH path fails Eggpool's real fixture despite Eggress's own regression suite:

1. capture the exact URI/failure class without secrets;
2. compare Eggpool target/URI semantics with the upstream fixture;
3. identify the missing compatibility behavior;
4. create a narrow upstream correction only if an actual Eggress defect is proven;
5. keep Eggpool on the last known-good state until a fixed published Eggress patch exists.

Do not ship a mixed 1.0.7 facade plus copied native SSH fallback without a separately documented emergency decision.

---

# Completion criteria

Plan 191 is complete only when:

1. Eggpool resolves published Eggress 1.0.7 without git/path overrides.
2. Every directly selected Eggress production/test/dev crate is on 1.0.7.
3. `eggress-ssh-fallback` is absent from Cargo features and source cfgs.
4. Default SSH is expressed as `ssh -> eggress-embed/ssh`.
5. `ProviderTcpConnector::new` sends SSH through `OutboundConnector`.
6. Production source no longer creates `SshSessionCache` or reconstructs SSH chain state.
7. `eggress-transport-ssh` is not a direct Eggpool dependency.
8. Direct implementation-crate source use is confined to `test-support` custom-root code.
9. That custom-root adapter owns no SSH state and preserves strict TLS verification.
10. Live SSH traversal passes through the ordinary constructor.
11. SSH auth failure remains correctly classified/redacted and timeout/cancellation remains bounded.
12. Proxy failure cannot fall back to direct egress.
13. No-default builds compile/lint/test, reject SSH fail-closed, and retain non-SSH proxy behavior.
14. Trojan custom-root success and untrusted-root failure remain green.
15. HTTP/SOCKS/SS/SSR/Trojan/multihop/direct behavior remains qualified.
16. Feature-tree evidence confirms facade ownership and no unexpected 1.0.6 package remains.
17. `cargo deny` passes.
18. Release footprint delta versus Plan 189 is recorded and any material regression explained.
19. Current documentation no longer describes the 1.0.6 workaround as active.
20. Plan 190 is annotated as historically complete but superseded for current SSH architecture by this plan.
21. No new Eggpool-specific Eggress API or insecure trust escape hatch was introduced.
22. This plan contains pushed-head closure evidence and is marked complete only after CI/qualification verification.

---

# Handoff note

The goal is not simply to consume a newer crate. The goal is to consume the upstream ownership fix that Eggpool originally needed, delete the local compatibility ownership that fix makes obsolete, and leave a smaller and more explicit trust/dependency boundary.

Once the stable facade passes Eggpool's real SSH fixture and the custom-root seam is cleanly test-only, stop. Further proxy architecture work requires a separate justification.
