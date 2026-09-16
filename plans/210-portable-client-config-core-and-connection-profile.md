# Plan 210: Portable client-config core and connection-profile contract

> **Status:** complete
>
> **Completed:** 2026-09-16 — `rust/crates/eggpool-client-config/` lands as the portable boundary; `rust/src/operations/integrations.rs` is the thin EggPool adapter; focused + serial workspace suites green locally (see commit).
>
> **Baseline:** EggPool `main` at `6997cdbb4cee4ec69c71cab15b53b039c7e3309a` (2026-09-16)
>
> **Parent:** Plan 209
>
> **Primary authority:** `rust/src/operations/integrations.rs`, `rust/src/operations/paths.rs`, `rust/src/cli.rs`, `rust/src/runtime.rs`, `rust/Cargo.toml`
>
> **Priority:** P0 — foundation for all remote client configuration work
>
> **Scope:** extract a small reusable Rust boundary for portable connection profiles and client configuration policy while preserving current local `configsetup` behavior and keeping server-side catalog/database access in the EggPool application.

## Objective

`rust/src/operations/integrations.rs` now contains both reusable client-configuration semantics and application-owned concerns such as EggPool config loading, database/catalog reads, server key resolution, LAN endpoint detection, and local CLI delivery. The remote-desktop feature needs the reusable part in a small crate that can be linked into both the EggPool executable and a desktop configurator without dragging in routing, SQLite, provider transport, Axum, or server lifecycle.

Introduce:

```text
rust/crates/eggpool-client-config/
```

as the portable configuration boundary.

The crate should own typed connection/profile schemas, bounded token encode/decode, client-neutral projected model types, target identification, pure rendering/mutation helpers, ownership-state types, hashing, and validation that does not require EggPool runtime state.

The EggPool application continues to own:

- loading `Config` and database/catalog/model-info facts;
- producing the conservative agent projection from authoritative server state;
- resolving server API-key policy;
- choosing the advertised endpoint;
- CLI presentation/clipboard behavior;
- HTTP serving;
- runtime paths specific to the proxy process.

Do not solve the extraction by moving all of `integrations.rs` into a crate.

---

# Workstream 1 — Inventory and classify the current integrations module

Before moving code, classify each item in `rust/src/operations/integrations.rs` as one of:

1. **portable policy** — can operate on typed data/files without EggPool runtime/database state;
2. **EggPool adapter** — converts `Config`/catalog/database facts into portable types;
3. **local lifecycle adapter** — current same-host `configsetup` path resolution and CLI-oriented state;
4. **delivery/presentation** — stdout, clipboard, hints;
5. **server-only projection loading** — database/config mutation/transcoder concerns.

Expected portable candidates include, after decoupling:

- normalized agent model/capability DTOs required by client renderers;
- Codex catalog serialization/validation policy;
- Codex provider configuration semantics;
- OpenCode provider/model serialization semantics;
- ownership field definitions;
- config hash/fingerprint helpers;
- pure text/document mutation primitives;
- connection profile types and codecs.

Expected application-owned items include:

- `build_integration_context()` / `build_endpoint_context()` and catalog loading;
- `resolve_server_key()` use;
- `detect_lan_ip()`;
- `Database`, `CatalogRepository`, `AccountRepository` access;
- transcoder enablement/mutation;
- current clipboard process invocation;
- current EggPool state-dir resolution.

Write the extraction so current public application behavior can continue through thin adapter functions. Avoid a broad compatibility facade that leaves duplicate implementations behind.

---

# Workstream 2 — Define portable data types

The crate must not depend on EggPool's database row types or application `Config`.

Provide a client-neutral projection type sufficient for Codex/OpenCode renderers. Reuse the semantics already established by Plan 200:

```rust
pub struct AgentModelProjection {
    pub public_id: String,
    pub display_name: String,
    pub capabilities: AgentModelCapabilities,
}

pub struct AgentModelCapabilities {
    pub context_tokens: Option<u64>,
    pub max_output_tokens: Option<u64>,
    pub input_text: bool,
    pub input_images: Option<bool>,
    pub reasoning: Option<AgentReasoningCapabilities>,
    pub function_tools: Option<bool>,
    pub freeform_tools: Option<bool>,
    pub deferred_tool_search: Option<bool>,
    pub responses: bool,
    pub websockets: bool,
}
```

If these exact existing types can move without pulling application dependencies, move them rather than creating nearly identical DTOs. If aggregation still needs application-only source metadata, keep aggregation in the application and define a wire DTO in the crate with explicit conversion.

The crate must preserve these semantic invariants:

- unknown capability stays unknown;
- no capability inference from model names;
- aliases expose conservative guarantees, not unions;
- no provider-private source metadata field exists in the portable public DTO;
- WebSocket/remote-compaction facts are not fabricated by the profile layer.

---

# Workstream 3 — Define `ConnectionProfileV1`

Create a versioned portable schema. Keep it intentionally small.

Suggested semantic shape:

```rust
pub struct ConnectionProfileV1 {
    pub schema: ProfileSchema,
    pub targets: Vec<ClientTarget>,
    pub proxy: ProxyReference,
    pub auth: AuthReference,
    pub integration_profile: IntegrationProfileReference,
    pub issuer: IssuerMetadata,
}
```

Equivalent JSON should resemble:

```json
{
  "schema": "eggpool.connection/v1",
  "targets": ["codex"],
  "proxy": {
    "base_url": "https://pool.example/v1",
    "wire": "responses"
  },
  "auth": {
    "mode": "bearer_env",
    "env": "EGGPOOL_API_KEY"
  },
  "integration_profile": {
    "endpoint": "/api/integrations/v1/profile",
    "schema": 1
  },
  "issuer": {
    "eggpool_version": "0.8.0"
  }
}
```

The exact field names can change during implementation, but the following are required:

- explicit schema major version;
- one or more supported target enums, never arbitrary executable names;
- absolute HTTP(S) EggPool base URL;
- protocol/wire fact required for setup;
- auth reference only, never a resolved credential;
- explicit remote integration-profile endpoint/schema;
- optional non-sensitive issuer/version metadata.

Do not put receiving-machine filesystem paths, shell commands, arbitrary environment assignments, client config fragments, provider credentials, or raw model catalogs into the normal profile.

### Schema evolution

Use a strict major-version boundary:

- known major + known required fields: accept;
- unknown major: reject with an actionable error;
- additive minor metadata may be ignored only when explicitly defined as optional.

Avoid untagged enum magic that could reinterpret future fields incorrectly.

---

# Workstream 4 — Define the `epc1` transport encoding

The copy/share representation should be shell-friendly and self-identifying:

```text
epc1.<base64url-no-padding payload>
```

The payload should be canonical UTF-8 JSON with optional lightweight compression if the dependency/size tradeoff is justified.

### Compression decision gate

Do not add a compression crate automatically. Measure representative profiles first. Because the default profile is intentionally small, base64url of canonical JSON may be adequate and has a smaller dependency/security surface.

If compression materially improves the command length, use one bounded, well-maintained Rust implementation and encode the compression algorithm in the prefix/schema rather than relying on autodetection. For example, either:

```text
epc1j.<base64url(canonical-json)>
```

or

```text
epc1z.<base64url(deflate(canonical-json))>
```

is preferable to ambiguous magic-byte probing. The operator-facing alias may remain `epc1` only if the codec version unambiguously fixes the algorithm.

At planning time there is no direct compression dependency in `rust/Cargo.toml`; preserve that unless measurements justify one.

### Bounds

Define constants and tests for:

- maximum encoded token length;
- maximum decoded payload bytes;
- maximum decompressed payload bytes if compression exists;
- maximum target count;
- maximum URL/string lengths;
- JSON nesting/collection bounds where necessary.

Decoding must allocate against bounded lengths and reject malformed base64, invalid UTF-8, duplicate/invalid required fields, unsupported schemes, whitespace/control-character injection, and unsupported schema versions.

A compressed payload must be protected against decompression bombs by enforcing output limits during decompression, not only after it finishes.

---

# Workstream 5 — Define the remote integration-profile DTO

Plan 211 will serve this object over HTTP; define its portable schema here so the server and `eggpool-connect` compile against one source of truth.

Suggested shape:

```rust
pub struct AgentIntegrationProfileV1 {
    pub schema_version: u32,
    pub revision: String,
    pub base_url: String,
    pub models: Vec<AgentModelProjection>,
    pub capabilities: IntegrationCapabilities,
}
```

Requirements:

- deterministic model ordering;
- bounded model count and total encoded bytes;
- deterministic revision/fingerprint derived from sanitized canonical content;
- no API key or provider credential;
- no provider-private source metadata;
- no prompt/response/user data;
- no local server filesystem paths;
- enough facts for current Codex/OpenCode rendering without another private server API.

Do not simply expose `IntegrationModel` or database row serialization. Define the public object deliberately.

---

# Workstream 6 — Define portable adapter interfaces

Avoid a large dynamic plugin system. A closed enum and small trait/pure-function boundary is sufficient.

A reasonable shape is:

```rust
pub enum ClientTarget {
    Codex,
    OpenCode,
}

pub struct ClientDetection {
    pub target: ClientTarget,
    pub version: Option<ClientVersion>,
    pub config_path: PathBuf,
    pub schema_variant: ClientSchemaVariant,
}

pub trait ClientAdapter {
    fn render(...);
    fn inspect(...);
    fn plan_mutation(...);
    fn verify_document(...);
    fn remove_owned(...);
}
```

Do not expose subprocess execution inside the crate. `eggpool-connect` owns running `codex debug models`, `codex doctor`, or `opencode models`; the crate can only return a verification plan/expected local artifacts.

This keeps policy testable and prevents the reusable library from becoming a process manager.

---

# Workstream 7 — Preserve local `configsetup` behavior during extraction

Refactor application code in small steps:

1. move/introduce portable types;
2. adapt existing projection into those types;
3. move pure Codex/OpenCode rendering/validation;
4. keep current lifecycle path behavior byte-equivalent where possible;
5. only then remove superseded implementation from `integrations.rs`.

The following contracts must not regress:

- `configsetup codex` emits `wire_api = "responses"`, WebSockets false, and `env_key = "EGGPOOL_API_KEY"`;
- current generated Codex catalog strict fields remain present;
- `CODEX_HOME` behavior stays intact for same-host lifecycle;
- OpenCode current renderer remains Responses-capable and secret-free;
- current managed apply/check/sync/remove behavior remains idempotent;
- standard `/v1/models` remains unchanged.

Do not combine this extraction with changes to request routing, Responses admission, wire codecs, or provider transport.

---

# Workstream 8 — Document dependency choices explicitly

A format-preserving Codex/OpenCode adapter may justify new narrow crates in Plan 213. Keep Plan 210 capable of landing before those choices where possible.

At planning time, candidate libraries include:

- `toml_edit` for lossless TOML editing;
- `jsonc-parser` or an equivalent narrowly scoped parser/editor for JSONC trivia-preserving mutation.

Do not add either solely because it is listed here. At implementation time review:

- latest compatible version and MSRV;
- license and `cargo deny` policy;
- transitive graph/duplicate versions;
- binary-size impact on both `eggpool` and `eggpool-connect`;
- whether the dependency can be scoped only to the portable client-config/helper surface.

Prefer a mature narrow dependency over maintaining a fragile custom parser, but do not pull a full JS/Node or general scripting runtime into Rust.

---

# Tests

Add focused unit tests in the new crate for:

- connection profile canonical serialization;
- token round-trip and deterministic encoding;
- malformed/oversize tokens;
- unsupported schema major;
- invalid URL schemes/authority/control characters;
- secret absence in profile/debug formatting;
- integration profile deterministic revision;
- maximum model/profile bounds;
- portable Codex/OpenCode renderers after extraction;
- no provider-private metadata leakage.

Update existing tests rather than duplicating them where ownership moved.

Required focused checks:

```bash
cargo test --manifest-path rust/crates/eggpool-client-config/Cargo.toml
cargo test --manifest-path rust/Cargo.toml --test operations_o005 -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --lib operations::integrations
cargo test --manifest-path rust/Cargo.toml --test cli_contract -- --test-threads=1
```

If `Cargo.toml`/lock changes:

```bash
cargo deny --manifest-path rust/Cargo.toml check
cargo tree --manifest-path rust/Cargo.toml -e features
cargo tree --manifest-path rust/Cargo.toml --duplicates
cargo build --manifest-path rust/Cargo.toml --locked --release
```

Then run strict Clippy and the serial workspace suite before closing the plan.

---

# Acceptance criteria

1. `rust/crates/eggpool-client-config/` exists as a small application-independent Rust boundary.
2. The crate does not depend on Axum, Tokio process/runtime features, SQLite, Eggress, provider transport, routing, quota, health, or EggPool database repositories.
3. Existing `AgentModelProjection` semantics have one authoritative implementation; no parallel Codex/OpenCode capability database is introduced.
4. `ConnectionProfileV1` and `AgentIntegrationProfileV1` are typed, versioned, bounded, deterministic, and secret-free.
5. The shareable token has a self-identifying versioned encoding and rejects malformed/oversize input before unbounded allocation.
6. Receiving-machine paths/commands cannot be supplied by the profile.
7. Existing same-host `configsetup codex/opencode` behavior and focused tests remain green through adapters to the shared crate.
8. No standard `/v1/models` behavior changes.
9. No Python/Node/client runtime dependency is added to the production EggPool server.
10. New direct dependencies, if any, pass license/advisory/source review and have documented binary-size/duplicate-graph impact.
11. `integrations.rs` is materially reduced in responsibility rather than merely forwarding to duplicated old/new implementations.
12. Documentation/architecture ownership is updated only after the code boundary lands.

## Handoff note

Keep this plan mechanical and boundary-focused. Do not add the network endpoint or desktop binary until the shared profile/types compile and existing `configsetup` behavior is preserved. The most important failure mode to avoid is creating two client-config engines: one in `integrations.rs` and another in the new helper.
