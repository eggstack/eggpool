# Plan 176 — Shared Model-Routing Parity, Release, and Closure

Date: 2026-09-11
Status: ready for handoff
Parent roadmap: `plans/173-shared-model-routing-crate-roadmap.md`
Depends on: Plans 174–175
Priority: P1 compatibility / closure
Execution target: GPT-5.6 Luna/Sol or comparable implementation model

## Objective

Close the shared semantic-routing extraction only after proving that the new crate is behaviorally identical for EggPool, consumable by Codegg at its lower MSRV, and does not disturb EggPool packaging, updates, dependency boundaries, or provider/account routing.

This is a bounded closure pass. Do not turn it into a crates.io publishing program, cross-repository CI platform, or further routing redesign.

## 1. Establish canonical shared compatibility vectors

Create a small set of stable test vectors owned by `eggpool-model-routing` and consumed directly or mirrored with exact expected values downstream. At minimum cover:

- a multi-route policy with non-lexicographic input labels to prove deterministic sorting/IDs;
- normalized whitespace in route descriptions;
- exact `model-router/v1` static policy bytes;
- exact configuration fingerprint;
- invalid default target;
- nested virtual target rejection;
- UTF-8 byte-limit boundaries;
- explicit session identity digest;
- automatic identity with a very large shared system/developer prefix and differing first-user turns;
- invalid/unknown route-ID rejection.

If affinity is shared, also include TTL expiration, LRU eviction, concurrent single-flight and cancelled-leader recovery.

Do not introduce a bespoke JSON protocol solely for tests if ordinary Rust fixtures/constants are sufficient.

## 2. Prove EggPool parity on the exact post-extraction head

Run the crate tests plus the full EggPool workspace tests. Explicitly verify behavior that sits immediately across the crate boundary:

- config parsing/adaptation produces the same compiled registry;
- virtual model IDs and `/v1/models` exposure are unchanged;
- selector/default/repair behavior is unchanged;
- sticky affinity and rehash continuity are unchanged;
- feature-off concrete requests still bypass semantic selection work;
- concrete target validation/transcoding still occurs after semantic resolution;
- provider/account eligibility, scoring, quota, health, retry/backoff and claims do not receive new semantic-routing inputs;
- target failure does not trigger semantic reselection.

No provider/account router source should move into the shared crate during closure.

## 3. Prove downstream MSRV and dependency isolation

Using Rust 1.81, verify the shared crate independently and then verify the Codegg consumer after the pinned dependency lands.

The shared crate's resolved dependency graph must not include EggPool application stacks such as Axum/Hyper/Tower, Eggress, SQLite, Clap, TOML/application config, or provider/catalog implementations.

If an optional Tokio affinity feature exists, prove both:

```text
default/core build: no Tokio dependency
feature build: bounded Tokio dependency only for affinity synchronization
```

If keeping Tokio out of default features would materially complicate the API for no practical gain, record the evidence and choose the simpler contract—but do not silently raise Codegg's MSRV.

## 4. Pin downstream dependency immutably

Codegg must not depend on EggPool `main`. Pin an immutable commit SHA or stable tag containing the shared crate.

For the first integration, a Git revision is acceptable and avoids creating a crates.io release process merely for one sibling project. Record the exact EggPool commit in the Codegg dependency and in closure evidence.

If future consumers justify crates.io publication, handle that as ordinary crate release/versioning work later. Do not block this extraction on publication.

## 5. Define API/versioning ownership

Add concise developer documentation in EggPool near the crate or architecture docs stating:

- EggPool owns the crate;
- `model-router/v1` is a semantic protocol contract distinct from crate semver;
- breaking public Rust API changes require downstream Codegg review/update;
- changing static policy/fingerprint semantics requires explicit compatibility consideration and likely a selector protocol version decision;
- provider/account routing is explicitly out of crate scope.

Keep the public API small. Before closure, review exported items and make helpers private where neither EggPool nor Codegg needs them. Avoid exposing internal cache structures simply because they were public in the monolithic module.

## 6. Requalify EggPool packaging/update contracts

The workspace conversion must not disturb the recently consolidated release/compatibility line.

Run current neutral repository validators, including the package/runtime boundary and release workflow/docs validators. Build the native EggPool release artifact/wheel through the current non-publishing path and prove that:

- package name remains `eggpool`;
- version authority remains `rust/Cargo.toml`;
- Maturin still selects the EggPool binary package;
- the shared library crate is not accidentally published as the Python package;
- installer/update artifact discovery is unchanged;
- exact-version Python<->Rust compatibility metadata/tooling is untouched.

Do not rerun a public publication or mutate PyPI as part of closure. The existing release qualification remains the authority for cross-era package transitions; this line only proves the workspace change did not break it.

## 7. Reconfirm dependency audit conclusions

Plan 170 remains closed. The extraction may remove `sha2` or other dependencies from the EggPool root only if they no longer have any root-level caller after moving code; Cargo can naturally reveal that. Do not reopen the broad dependency audit.

Record the shared crate's direct dependency list and ensure no accidental heavyweight dependency entered it. Binary-size/package-count changes are informational, not acceptance thresholds.

## 8. Avoid permanent cross-repo CI coupling

Do not create a workflow in EggPool that clones Codegg on every push, or vice versa. That would make each repository's ordinary CI availability dependent on the other repository.

Preferred maintenance model:

- EggPool CI tests the shared crate and EggPool adapter;
- Codegg CI tests its pinned dependency and adapter;
- when changing the shared public API, update/test Codegg deliberately before moving its pin;
- optional manual/release-time parity checks may test both repositories together.

This is sufficient for two closely related public projects and avoids over-engineering.

## 9. Verification commands

Use the repository-defined current equivalents if commands evolve. Expected EggPool closure set:

```bash
cargo fmt --manifest-path rust/Cargo.toml --all -- --check
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml --locked --release
cargo tree --manifest-path rust/crates/eggpool-model-routing/Cargo.toml
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run python scripts/check_release_catalog.py
uv run python scripts/validate_release_identity.py
uv run python scripts/validate_release_workflow.py .github/workflows/release.yml
uv run python scripts/validate_release_docs.py
uv run python scripts/validate_runtime_package_boundary.py
git diff --check
```

Additionally run the current local/native package build or release rehearsal necessary to prove Maturin still packages EggPool correctly. Do not publish.

In Codegg, run its normal formatting/Clippy/tests using the pinned shared crate and explicitly run a Rust 1.81 check for the consuming package(s).

## 10. Closure evidence

When implementation is complete, append concise evidence to Plans 173–176 containing:

- EggPool extraction commit;
- shared crate dependency/MSRV result;
- Codegg integration commit and pinned EggPool revision;
- canonical policy/fingerprint vector result in both repositories;
- EggPool full workspace test/Clippy/build results;
- package/release validator results;
- Codegg test/MSRV results;
- confirmation that no account/provider routing moved into the crate;
- confirmation that no new permanent cross-repository CI workflow was added.

If Codegg integration reveals that only compilation/validation is worth sharing and async affinity is not, treat leaving affinity application-owned as a successful scope reduction rather than a missing feature.

## Acceptance criteria

- Shared vectors produce identical policy bytes/fingerprints in EggPool and Codegg.
- Shared crate and Codegg consumer compile with Rust 1.81.
- EggPool remains Rust 1.88/edition 2024 and passes strict workspace gates.
- Codegg pins an immutable shared-crate revision/tag.
- Shared crate's dependency graph remains small and application-independent.
- EggPool provider/account routing semantics and source ownership remain application-local.
- Codegg durable provider-connection/session semantics remain unchanged.
- Maturin/native EggPool packaging, package identity/version authority, updater/release validators and cross-era compatibility tooling remain intact.
- No public publication is required for closure.
- No permanent cross-repo CI coupling is introduced.
- Plans 173–176 are marked complete with exact implementation/verification evidence.

## Definition of done

This line is closed when the shared semantic-routing crate has one clear owner, two verified consumers, a stable bounded protocol/API boundary, compatible MSRV, unchanged EggPool package/update behavior, and no duplication or leakage of infrastructure routing across repository boundaries.