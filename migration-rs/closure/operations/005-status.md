# O005 Closure — Agent Integration and `configsetup` Generation

Status: closed

Implementation commit: [`59591bca254fc5d71a55fbf750e9f0eb5189aba8`](https://github.com/eggstack/eggpool/commit/59591bca254fc5d71a55fbf750e9f0eb5189aba8)

Plan: [O005 — agent integration and `configsetup` generation](../../implementation/operations/005-agent-integration-config-generation.md)

## Acceptance evidence

`rust/src/operations/integrations.rs` now owns one bounded, target-neutral
integration context and small renderers. The runtime dispatcher maps every
current F003 `configsetup` parser variant to that service; it does not shell
out to Python or create a second configuration/runtime mutation path.

## All-target parity matrix

| F003 target | Rust command | Output contract | Model/config behavior | Result |
|---|---|---|---|---|
| OpenCode | `configsetup opencode` | JSON to stdout; clipboard best effort; key is explicit in generated provider config | Sorted model map, limits, reasoning, and variants | Pass |
| Claude Code | `configsetup claude-code` | Clipboard-only; no key-bearing stdout | `{api_key, base_url}` endpoint configuration | Pass |
| Aider | `configsetup aider` | Shell exports; optional model command | POSIX-safe quoting and optional model selection | Pass |
| Codex | `configsetup codex` | TOML env-key reference; generic secret-safe delivery | Responses wire profile and optional top-level model | Pass |
| Qwen Code | `configsetup qwen-code` | JSON provider fragment | OpenAI-compatible provider, optional model | Pass |
| Kilo | `configsetup kilo` | JSON OpenAI-compatible fragment | `apiBase`, key, and bounded context lengths | Pass |
| Continue | `configsetup continue` | YAML-compatible JSON output | Model entry with API base/key and optional model | Pass |
| Cline | `configsetup cline` | JSON settings fragment | OpenAI provider fields and optional model | Pass |
| Roo Code | `configsetup roo-code` | JSON settings fragment | Same OpenAI-compatible field contract as Cline | Pass |
| Goose | `configsetup goose` | Shell exports | Provider base URL/key and optional model | Pass |
| OpenHands | `configsetup openhands` | Shell exports | LLM base URL/key and optional model | Pass |

The O005 integration test iterates the complete Rust target set, and the
renderer tests cover structured escaping, shell quoting, optional models, and
URL normalization. The service also merges enabled-provider static models with
the local catalog, applies configured capability/limit overrides, and falls
back to static configuration when the catalog is unavailable.

## Output, write, and secret matrix

| Mode | Behavior | Safety evidence |
|---|---|---|
| Default generic delivery | Clipboard when available; otherwise no secret-bearing stdout | `--print-secret` is required to print generic snippets, including Codex's env-reference artifact |
| `--print-secret` | Prints the generated snippet explicitly | Covered by delivery regression test |
| `--no-clipboard` | Skips clipboard process invocation | Covered by deterministic delivery tests |
| `--output PATH` / `--write` | Creates parent directories and writes one trailing newline | Atomic temp-file sync/rename; existing files require `--force` |
| `--force` | Replaces an existing differing file | Creates a timestamped `.eggpool.bak.*` before replacement and preserves existing mode |
| Generated server key | Resolves inline key, explicit env-owned key, or persists a generated key through O004 mutation | Key is never placed in errors or custom `Debug` output |
| Config mutation | Enables transcoding only for the required Anthropic-only provider case | Reuses `config_mutation` and reports restart-if-running outcome |

OpenCode and Claude Code retain their frozen special delivery behavior:
OpenCode emits its JSON configuration and attempts clipboard delivery, while
Claude Code does not print the endpoint key. Clipboard execution is bounded,
uses a supported local utility only, and does not inherit stdin/stdout/stderr.

## Verification commands actually run

```text
rtk cargo fmt --manifest-path rust/Cargo.toml -- --check PASS
rtk cargo clippy --manifest-path rust/Cargo.toml --all-targets -- -D warnings PASS
rtk cargo test --manifest-path rust/Cargo.toml --test operations_o005 --test operations_o004 --test cli_contract -- --test-threads=1 PASS (15)
rtk cargo test --manifest-path rust/Cargo.toml --all-targets -- --test-threads=1 PASS
rtk uv run pytest tests/unit/test_integrations.py tests/integration/test_api_key_e2e.py tests/integration/test_e2e_key_flow_and_config.py tests/migration_rs -q --tb=short --maxfail=1 PASS (302 passed, 3 skipped)
rtk git diff --check PASS
```

The Rust O005 tests include assertions that generated snippets and custom
context debug output do not leak credentials under default delivery. No
provider network calls are required by the implementation or these checks.

## Dependency review and unresolved findings

O005 depends on accepted O004 and reuses its atomic TOML mutation, key
resolution, and restart/apply authority. It adds no database schema, provider
HTTP client, control endpoint, Python fallback, or new third-party crate;
Tokio's existing dependency only gains its `process` feature for bounded local
clipboard helpers. The Rust service reads the existing catalog/database and
configuration models without changing their schema or ownership rules.

No unresolved O005 correctness, security, data-loss, compatibility, or
resource finding remains. Broad OS/SBC coverage, live-provider verification,
Rust-default distribution, and Python retirement remain M10-M12 work. A later
defect must be recorded as a new corrective plan; this record is append-only.

## Dependency transition

O005 is removed from the dependency-ready section and recorded as completed in
the operations README, handoff sequence, roadmap, registry, and this closure
record. O006 is promoted to the sole dependency-ready plan because its hard
dependency is now accepted. O007-O010 remain queued behind their direct
predecessors. No future plan beyond O006 can be unblocked by O005 alone.

