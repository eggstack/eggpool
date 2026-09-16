# Plan 209: Remote coding-client configuration roadmap

> **Status:** ready for implementation
>
> **Baseline:** EggPool `main` at `6997cdbb4cee4ec69c71cab15b53b039c7e3309a` (2026-09-16)
>
> **Parent context:** Plans 198–208, especially Plan 200 (`AgentModelProjection` and managed Codex/OpenCode lifecycle) and Plan 206 (real-client qualification)
>
> **External audit baseline:** Codex CLI 0.154.0 qualification recorded by Plan 206; OpenCode 1.18.30 qualification recorded by Plan 206; current OpenCode V2 provider/config migration documentation re-checked 2026-09-16
>
> **Scope:** make a LAN/headless EggPool deployment easy to connect to Codex and OpenCode running on separate desktop machines, without turning EggPool into OpenCodex or moving agent-runtime ownership into the proxy.

## Executive summary

Plans 198–208 closed the protocol and local integration work needed for Codex and OpenCode. EggPool now owns a conservative provider-neutral model projection, a generated Codex catalog, a Responses-capable OpenCode renderer, idempotent managed `configsetup` lifecycle operations, drift detection, safe removal, and deterministic/live client qualification.

The remaining usability gap is topology: `configsetup --apply` operates on the same machine as EggPool, while the intended deployment is often a headless SBC/server hosting EggPool and one or more desktops hosting Codex/OpenCode. Copying snippets manually works, but it does not provide a safe, repeatable, version-aware installation path for a workgroup sharing one proxy.

Add a narrow remote-configuration facility:

```text
Headless EggPool
  eggpool configremote codex
          |
          v
  secret-free connection profile token
  epc1.<base64url(... )>
          |
          | copy/share
          v
Desktop
  eggpool-connect install --profile epc1....
          |
          +-- detect client + version
          +-- fetch current EggPool integration projection
          +-- show proposed mutation
          +-- create byte-exact backup
          +-- mutate only EggPool-owned fields
          +-- validate client-native config/model discovery
          `-- rollback automatically on failure
```

The portable token is a **connection profile**, not a serialized Codex/OpenCode config file. Client-specific schema/version decisions belong on the desktop after the installed client is known. The token is secret-free by default and therefore safe to reuse across multiple PCs. Credentials remain separate.

The implementation is split into Plans 210–214.

---

## Goals

1. `eggpool configremote <target>` on an SBC prints a small reusable connection profile and copy/paste bootstrap commands for desktop systems.
2. The profile identifies the EggPool endpoint, target client, auth reference, profile schema, and refresh metadata without embedding the EggPool server key.
3. A native `eggpool-connect` desktop helper configures supported clients transactionally on macOS, Windows, and Linux.
4. Existing `configsetup` and remote setup share the same client configuration policy rather than maintaining two Codex/OpenCode implementations.
5. Every mutating desktop operation creates recoverable state before modification and automatically rolls back when post-write validation fails.
6. OpenCode JSONC and current schema/version differences are handled without destroying comments or unrelated settings.
7. Standard `/v1/models` remains the standard OpenAI-compatible model-list surface. Rich remote integration facts use a separate authenticated EggPool-specific schema.
8. The feature remains a proxy deployment/configuration feature. EggPool does not acquire an agent loop, Codex account emulation, client process interception, plugin execution, conversation state, or other OpenCodex product responsibilities.

## Non-goals

Do not:

- install or upgrade Codex/OpenCode as part of normal setup;
- run Codex/OpenCode as a child process or daemon;
- emulate Codex-private OpenAI account/session APIs;
- copy prompts, instructions, approval policy, sandbox policy, conversation history, tool execution, or compaction ownership into EggPool;
- put the EggPool API key, provider credentials, raw prompts, model responses, or backup contents into the portable profile;
- overload `/v1/models` with Codex-specific metadata;
- require Python, Node, Codex, OpenCode, or an AI SDK as an EggPool server runtime dependency;
- make Windows proxy support a prerequisite for shipping a Windows desktop configurator.

---

# Current-state findings

## 1. Most client policy already exists

`rust/src/operations/integrations.rs` currently owns:

- `IntegrationModel`, `AgentModelCapabilities`, `AgentModelProjection`;
- conservative aggregation of alias capabilities;
- Codex TOML and generated catalog renderers;
- OpenCode provider/model rendering;
- Codex/OpenCode config path resolution;
- ownership manifests, hashes, atomic writes, drift checks, apply/sync/check/remove;
- format-specific validation.

This is the correct semantic baseline, but it is too application-coupled to link directly into a small desktop utility because the same module also loads EggPool config/database state and owns server-side integration context construction.

The remote feature should extract only the portable client policy into a reusable crate. EggPool server-side catalog/SQLite/provider access stays in the application.

## 2. `configsetup` is local-machine oriented

The current lifecycle resolves `~/.codex/config.toml`, `CODEX_HOME`, XDG OpenCode paths, and EggPool's own local state directory. That is correct when client and proxy are co-located. It does not solve a headless proxy + desktop client topology.

`configremote` must therefore describe connection intent rather than attempt to write a remote user's filesystem.

## 3. Bind address is not an advertised client endpoint

`rust/src/config.rs::ServerConfig` owns the listen host/port. Integration context currently derives a LAN address with `detect_lan_ip()` unless an explicit `--host`/`--base-url` override is supplied.

For reusable remote profiles, the operator needs a stable advertised endpoint independent of the bind address. Examples include LAN IP, mDNS/DNS, Tailscale/WireGuard address, or reverse-proxy HTTPS URL.

Add an explicit integration/public endpoint setting rather than deriving a reusable profile silently from the bind address.

## 4. Literal catalogs are the wrong transfer unit

The Codex catalog is intentionally bounded but may still contain hundreds of model entries. Embedding it in a shell command creates large fragile tokens and immediately makes the copied command stale when the model inventory changes.

The normal portable token should therefore contain endpoint/profile metadata only. The desktop helper authenticates to EggPool and fetches a current provider-neutral integration profile. An explicit bounded snapshot/offline mode may be added later, but is not the default.

## 5. Base64/compression does not protect credentials

The workgroup use case implies the command may be copied into shell history, chats, tickets, notes, screenshots, and clipboard managers. A base64-encoded API key would still be a leaked API key.

The default connection profile must contain no credentials. The desktop helper obtains a credential separately for profile fetch/verification and may persist it only through an explicit, reversible action.

## 6. OpenCode schema churn belongs in an adapter

Current production EggPool is qualified against OpenCode 1.18.30 and renders the V1-style `provider` / `npm` / `options` shape. Current OpenCode V2 documentation moves custom providers toward plural `providers`, `package`, and `settings`, with Responses-specific provider packages available.

The transferable profile must not freeze either shape. `eggpool-connect` must detect the installed client/version/schema and choose an adapter validated against that contract.

---

# Target architecture

```text
                           EggPool host

 Config + catalog + model-info + routing facts
                     |
                     v
         provider-neutral projection
                     |
        +------------+-------------+
        |                          |
 configsetup local          integration profile API
        |                          |
 existing local flow        GET /api/integrations/v1/profile
                                   ^
                                   |
                    ConnectionProfileV1 / epc1 token
                                   |
                            eggpool configremote

----------------------------------------------------------------

                         Desktop host

                      eggpool-connect
                           |
         +-----------------+------------------+
         |                 |                  |
       Codex          OpenCode V1        OpenCode V2+
       adapter           adapter             adapter
         |                 |                  |
         +--------- transactional engine -----+
                           |
              backup -> mutate -> verify
                           |
                    rollback on failure
```

The shared Rust crate contains profile types/codecs, portable config adapters, ownership/backup metadata types, and pure mutation/validation policy. The EggPool application adapts its server-side projection into those types. `eggpool-connect` adapts local files/client versions into the same policy.

---

# Implementation sequence

## Plan 210 — Portable client-config core and connection-profile contract

Extract a small `rust/crates/eggpool-client-config/` boundary from the pure parts of `operations/integrations.rs`. Define versioned profile schemas, bounded token encoding/decoding, portable target adapter contracts, and reusable config mutation primitives.

This plan must preserve existing `configsetup` behavior before any remote command is added.

## Plan 211 — `configremote`, advertised endpoint, and remote integration-profile API

Add an explicit advertised integration base URL, a read-only `eggpool configremote <target>` command, and an authenticated EggPool-specific endpoint exposing current sanitized provider-neutral integration facts.

The command emits secret-free `epc1` tokens and copy/paste bootstrap commands. `/v1/models` is unchanged.

## Plan 212 — `eggpool-connect` transactional installer and recovery

Add the small desktop-side native binary. Implement plan/install/verify/remove/backups/restore, OS/client detection, credential prompting, profile fetching, byte-exact backups, atomic writes, and automatic rollback after failed validation.

## Plan 213 — Current Codex/OpenCode portable adapters

Move/upgrade the real client adapters onto the shared core. Preserve Codex behavior and current strict catalog validation. Make OpenCode mutation JSONC-preserving, restore a previous EggPool provider value correctly, and select current V1/V2 configuration shape based on verified installed-client behavior.

## Plan 214 — Bootstrap, release portability, and live qualification

Publish the desktop helper for supported desktop targets, add version-pinned verified POSIX/PowerShell bootstrap entry points, and qualify the complete headless-proxy-to-desktop flow with disposable homes/configs on Linux/macOS/Windows.

---

# Cross-cutting requirements

## Portable profiles are data, never executable instructions

Profile fields are typed values only: schema, target(s), base URL, auth mode/reference, profile endpoint/revision, and optional bounded metadata. They cannot contain shell fragments, filesystem paths to write on the receiving machine, arbitrary command names, or executable hooks.

The desktop decides local paths using its client adapter. This prevents a malicious or malformed remote token from directing writes outside supported config/state roots.

## Bounded decoding

Before decompression/parse, enforce explicit limits for:

- token character count;
- base64-decoded bytes;
- decompressed bytes;
- JSON nesting/collection/string lengths where the chosen parser requires extra guarding;
- target count;
- endpoint length;
- remote projected model count and response bytes.

Reject unknown future major schema versions. Ignore/retain forward-compatible minor fields only when the schema explicitly permits it.

## Secret-free by default

`ConnectionProfileV1` carries an auth **reference**, such as `EGGPOOL_API_KEY`, not its resolved value. No output mode silently adds credentials.

If a future explicit secret-bearing export is added, it must be a separately named opt-in operation with unmistakable warnings and must not become the workgroup/shareable default. It is outside Plans 209–214 unless required by qualification.

## Separate server and client ownership

The EggPool server remains authoritative for model projection and public endpoint facts. The desktop remains authoritative for local client installation paths and installed-client version.

Neither side guesses facts owned by the other.

## Fail closed on ambiguous writes

A helper must refuse mutation when it cannot safely determine:

- the intended client;
- the effective config path;
- a supported client schema/version;
- parseability of existing config;
- ownership of an existing EggPool-managed block;
- backup completion;
- atomic replacement capability.

`--force` may override known drift only; it must not bypass malformed profile, unsafe path, failed backup, unsupported schema, or failed post-write validation.

## Preserve current protocol scope

Remote configuration does not change the stateless HTTP/SSE Responses contract, current WebSocket policy, remote-compaction policy, routing, provider transport, or request persistence. No request/coordinator/wire changes belong in this roadmap unless live qualification exposes a genuine client/proxy compatibility defect.

---

# Verification strategy

Focused implementation checks begin with:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --test operations_o005 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib operations::integrations
```

New crate/binary tests must be directly runnable and must not require live credentials for deterministic coverage. Changes to dependencies additionally require:

```bash
cargo deny --manifest-path rust/Cargo.toml check
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
cargo build --manifest-path rust/Cargo.toml --locked --release
```

Before closure, run the serial workspace suite and the real-client qualification matrix from Plan 214.

---

# Roadmap acceptance criteria

This roadmap is complete when all of the following are true:

1. A headless EggPool host can run `eggpool configremote codex` or `eggpool configremote opencode` without touching a desktop filesystem.
2. The returned shareable token contains no resolved API key or provider credential.
3. The same token can configure multiple authorized desktops that can reach the same EggPool instance.
4. The desktop helper detects the installed client contract and does not rely on server-side assumptions about desktop paths/version.
5. Current model metadata is obtained from a versioned authenticated EggPool integration-profile surface; `/v1/models` is unchanged.
6. Existing local `configsetup codex/opencode` behavior remains supported and uses the same underlying portable policy where applicable.
7. Every desktop mutation has a completed byte-exact backup before the first write.
8. A failed parse, network fetch, credential check, filesystem mutation, or client-native post-write verification leaves/restores the original client state.
9. Codex continues to pass current `codex debug models` and `codex doctor --json` qualification.
10. OpenCode configuration preserves comments/unrelated settings and handles the current supported schema/version family without lossy whole-file rewrites.
11. Backup/restore/remove are idempotent, bounded, user-owned, and do not expose backup contents or credentials in logs/stdout.
12. Desktop bootstrap downloads are version-pinned and integrity-verified; no mutable `main` script is executed as the configuration engine.
13. Windows desktop-helper support does not imply Windows EggPool proxy support.
14. No Codex/OpenCode runtime, Node, Python application runtime, or agent-harness dependency is added to the EggPool server.
15. Architecture/docs explicitly retain the boundary: EggPool is a model proxy plus connection configurator, not OpenCodex.

## Handoff note

Implement Plans 210–214 in order unless a dependency can be developed independently without changing the shared profile/adapter contracts. Re-check current Codex/OpenCode source/documentation immediately before implementing each client adapter. Treat client schema churn as adapter/test work; do not leak client-specific fields into routing, catalog persistence, or the standard OpenAI model-list API.
