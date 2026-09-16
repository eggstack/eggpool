# Plan 208: Coding-agent closure checklist

> **Status:** READY FOR IMPLEMENTATION
>
> **Parent:** Plan 203
>
> **Priority:** Final checklist
>
> **Scope:** compact completion checklist for the coding-agent compatibility closure pass. This plan contains no new architecture or feature work.

## Checklist

### Baseline

- [ ] Record Eggpool commit/version.
- [ ] Record current Codex version.
- [ ] Record current OpenCode version.
- [ ] Record OS/architecture and non-secret provider/wire path.

### Codex

- [ ] `configsetup codex --dry-run` is non-mutating.
- [ ] `configsetup codex --apply` succeeds in isolated `CODEX_HOME`.
- [ ] `configsetup codex --check` reports clean state.
- [ ] generated `model_catalog_json` loads.
- [ ] expected Eggpool model/alias is selectable.
- [ ] conservative context/output/reasoning metadata is visible/accepted.
- [ ] streamed text request succeeds.
- [ ] ordinary client-executed tool loop succeeds.
- [ ] deferred `tool_search` live test succeeds or is documented as not stably live-exercisable with deterministic conformance retained.
- [ ] current compaction behavior is identified and works without Eggpool conversation persistence.

### OpenCode

- [ ] `configsetup opencode --dry-run` is non-mutating.
- [ ] `configsetup opencode --apply` succeeds in isolated config.
- [ ] `configsetup opencode --check` reports clean state.
- [ ] Eggpool provider/models appear.
- [ ] Responses-capable provider runtime is accepted.
- [ ] environment API-key interpolation is accepted.
- [ ] context/output metadata is accepted.
- [ ] text request succeeds.
- [ ] ordinary client-executed tool loop succeeds.

### Managed config lifecycle

- [ ] missing/empty config fixture passes.
- [ ] unrelated user config is preserved.
- [ ] third-party provider config is preserved.
- [ ] repeat `--apply` is idempotent.
- [ ] `--check` is read-only.
- [ ] `--sync` changes only Eggpool-owned material.
- [ ] managed drift is detected/refused as designed.
- [ ] `--remove` removes/restores only Eggpool-owned material.
- [ ] generated config/catalog contains no secret value.

### Status

- [ ] healthy proxy/provider output qualified.
- [ ] `status --json` schema/version qualified.
- [ ] provider appears exactly once per configured provider.
- [ ] partial degradation semantics qualified where safely reproducible.
- [ ] reachable-but-unready exit/status qualified.
- [ ] unreachable proxy offline fallback/exit status qualified.
- [ ] invoking `status` causes no outbound provider traffic or health mutation.
- [ ] output contains no credentials/raw error bodies/private request content.

### Corrective policy

- [ ] each Eggpool-owned live failure has a deterministic regression.
- [ ] fixes are at the narrowest existing ownership boundary.
- [ ] no provider-specific workaround weakens global protocol semantics.
- [ ] no new architecture is added under the guise of closure.

### Quality gates

- [ ] `cargo fmt --manifest-path rust/Cargo.toml --all -- --check`
- [ ] `cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings`
- [ ] no-default-feature check/clippy passes.
- [ ] full workspace tests pass serially.
- [ ] `codex_responses_compat` passes.
- [ ] `codex_compaction_compat` passes.
- [ ] `operations_o005` passes.
- [ ] `status_command` passes.
- [ ] integration/status focused library tests pass.
- [ ] release build succeeds.
- [ ] `git diff --check` succeeds.
- [ ] final GitHub CI succeeds.

### Evidence/plan closure

- [ ] concise secret-free qualification evidence committed.
- [ ] Plan 199 closure evidence updated.
- [ ] Plan 200 closure evidence updated.
- [ ] Plan 201 closure evidence updated.
- [ ] Plan 202 closure evidence updated.
- [ ] stale user/developer docs corrected only where necessary.
- [ ] Plan 198 closed last.
- [ ] remaining intentional deferrals explicitly listed.
- [ ] Plan 206 marked not needed or completed.
- [ ] Plan 203 closure acceptance criteria all satisfied.

## Final condition

Do not declare the coding-agent proxy compatibility milestone closed until every applicable checkbox above is satisfied or has a written, evidence-backed `N/A`/intentional-deferral rationale in the qualification record.
