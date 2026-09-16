# Plan 204: Codex deferred-tool compatibility and conformance closure

> **Status:** complete
>
> **Closes:** `plans/201-codex-deferred-tool-compatibility-and-conformance.md`
>
> **Scope:** narrow provider-neutral client-executed `tool_search` bridge across function-capable non-Responses upstreams, native preservation where available, extended coding-agent conformance without turning Eggpool into a tool executor.

## What was built

- Provider-neutral `CanonicalToolKind::DeferredSearch` in `rust/src/wire/ir.rs` (client-executed only; hosted/server search stays native-only). Bounded `query`/`limit` validation (`validate_tool_search_arguments_string/value`), default Codex schema (`query` required, default limit 8), distinct item-ID vs `call_id` handling. `function_parameters()` reuses the exact search schema; `to_responses_value()` emits `tool_search` with `execution: client`.
- `rust/src/request/admission.rs`: client `tool_search` declarations, `tool_search_call`/`tool_search_output` history items decoded to canonical deferred blocks; hosted/server forms return `None` so they count as native blockers. `native_feature_summary()` excludes portable client search from cross-surface blockers. Malformed search arguments fail closed (`InvalidField`); output `tools` bounded (`MAX_TOOL_SEARCH_TOOLS`, `MAX_TOOL_SEARCH_OUTPUT_BYTES`). Declaration-scoped identity: ordinary `function` named `tool_search` stays `Function`.
- `rust/src/wire/additional_codecs.rs`: Responses request/response codecs encode/decode deferred search (`response_tools()`, `tool_search_call`/`tool_search_output` items, finite `decode_responses_response()` handling for native finite forwarding). `defer_loading` preserved on Responses function tools.
- `rust/src/wire/adaptation.rs` + `rust/src/wire/mod.rs`: `deferred_tool_search_wrapped_as_function` notice and reusable `supports_deferred_tool_search()` capability fact (all built-in surfaces: native Responses preserves, others wrap).
- `rust/src/wire/runtime.rs`: pre-dispatch gate rejects deferred requests on unsupported targets (`tools.tool_search`, `UnsupportedSemanticFeature`); finite `classify_freeform_output()` extended to declaration-scoped deferred classification with malformed-wrapper rejection (never forwards wrapper JSON as native calls).
- `rust/src/wire/stream.rs`: translated encoder emits authoritative `tool_search_call` done items (`execution: client`, bounded object, distinct IDs); argument fragments accumulate silently by source index/call ID (no invented delta grammar); malformed stream arguments fail as `MalformedProviderEvent`.
- `rust/src/operations/integrations.rs`: `AgentModelCapabilities` gains conservative `freeform_tools`/`deferred_tool_search` (unknown stays unknown, intersection aggregation, no name inference). Renderers do not optimistically advertise deferred support.
- `rust/tests/codex_responses_compat.rs`: 8 new deterministic cases (native preservation, wrapper declaration, call/output round trips, stable IDs, interleaved accumulation, parallel function+freeform+search, malformed fail-closed, ordinary-name non-reclassification, hosted pre-dispatch rejection, future-event preservation) with provenance `4701aa4b4239c70063ab6f2fcb835324f9c109f4` plus existing `508a006d...`/`e4a8539b...` markers and the Workstream 10 maintenance policy. No Codex/OpenCodex/OpenCode runtime dependency.

## Evidence

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test codex_responses_compat -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_codecs -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_stream -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_runtime -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_qualification -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --test wire_adaptation -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml --no-default-features -- --test-threads=1
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run python scripts/validate_release_docs.py
uv run python scripts/validate_runtime_package_boundary.py
git diff --check
```

Focused targets pass locally (`codex_responses_compat` 15 tests, `wire_codecs` 11, `wire_stream` 18, `wire_runtime` 8, `wire_qualification` 16, `wire_adaptation` 6). Full serial workspace suite passes. No-default feature guard passes. Python tooling (`ruff`, `pyright`, `pytest` 75 passed, 1 skipped) passes. Release doc/boundary validators pass.

Live Codex smoke (`scripts/smoke_codex_compat.sh`) unchanged by design: deterministic in-process coverage carries `tool_search`; live smoke still qualifies text + shell-tool loop only and returns 77 without credentials. No new live dependency on plugin/app ecosystems.

## Docs updated

- `docs/stateless-responses.md`: deferred-search wrapper, `tool_search_call` streaming, hosted stays native-only, Codex remains executor.
- `docs/transcoding.md`: canonical deferred-search subset, wrapper mapping, declaration-scoped identity.
- `README.md`: deferred portability note, hosted stays native-only.
- `architecture/deep-dive-transcoder.md` + `architecture/deep-dive-request-lifecycle.md` + `architecture/overview.md`: `DeferredSearch` ownership, silent accumulation, stable IDs.
- `.opencode/skills/architecture/SKILL.md`: deferred distinction + streaming rule.
- `.opencode/skills/development/SKILL.md`: `codex_responses_compat` deferred coverage.

`AGENTS.md` unchanged (no new module boundary; `rust/src/wire/` + `rust/src/request/` remain owners; no deep-dive duplication per repo rule).

## Acceptance mapping (Plan 201 criteria 1-12)

1. Current shapes captured in source-provenanced fixtures before translation — done (`tool_search_declaration()`, call/output items, hosted negatives).
2. Native Responses preserves without re-encoding — done (byte-exact native test, alias-only model rewrite, unknown-event preservation).
3. Qualified function-capable non-Responses route carries declaration -> call -> local execution -> output -> continuation — done (Chat wrapper + continuation + stream + finite classification tests).
4. Declaration-scoped identity; ordinary `tool_search` function never misclassified — done (dedicated test).
5. `call_id` vs item IDs distinct/stable — done (stream + parallel assertions).
6. Outputs stay tool outputs, not user text — done (function-result projection test).
7. Parallel/interleaved calls do not cross-contaminate — done (3-way parallel + split-delta tests).
8. Known-incompatible targets rejected before I/O — done (hosted bare/server rejected via `native_preservation_notices` + `supports_deferred_tool_search` gate).
9. No search/plugin/MCP execution — done (transport-only; documented).
10. Existing function/freeform + unknown-event preservation green — done (all 7 pre-existing compat tests + wire suites pass).
11. Capability metadata advertises deferred only where guaranteed — done (conservative `None` + intersection; no optimistic renderer claims).
12. No new production dependency — done (`Cargo.toml`/`Cargo.lock` untouched).
