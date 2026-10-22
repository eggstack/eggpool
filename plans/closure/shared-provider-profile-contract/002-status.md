# Shared Provider Profile Contract Milestone 002 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/shared-provider-profile-contract/002-opencode-go-current-endpoint-wire-hints.md`

Source subsystem roadmap:

- `plans/subsystems/shared-provider-profile-contract-roadmap.md#milestone-002--opencode-go-wire-hints-for-the-current-endpoint-table`

Repository baseline reviewed: `e48f021e`

Implementation commits or pull requests:

- `ffac612` — Add the four current OpenCode Go wire hints and guard the reviewed record against catalog drift

Downstream immutable revision for the registered CodeGG consumer: `ffac612`

## 1. Executive finding

The shared OpenCode Go model-wire map now agrees with the **current**
first-party endpoint table, and the agreement is guarded rather than asserted
once.

The first-party endpoint table (re-read 2026-10-22) places 29 models. Twenty-eight
of them land on one of the three surface families the shared contract models;
those 28 now all carry a non-fixed hint. The 29th, `gemini-4-argon-contributor`,
lands on a Google Generative AI endpoint family the contract does not model, so
it stays deliberately unresolved — answering it would change `eggpool-wire`'s
surface vocabulary and codec ownership, which is an ADR, not a metadata edit.
Four placements the table had gained since the M001 review (`beam-free`,
`minimax-m3.1-flash-free`, `step-5-preview`, `claude-haiku-5-5`) were missing
from the wire map and are now hinted, which is the capability this milestone
existed to deliver: a consumer selecting either free route previously failed
closed at `wire_unresolved` even though the provider serves both.

The six entries the current table no longer places were **not** deleted. A
placement that is not restated is not a retraction, and a stale hint costs a
consumer nothing, while a missing hint for a model the provider does serve is
what breaks one. Each carries a recorded disposition and reason.

The second half of the milestone is the part that prevents a recurrence. The
old guard compared the reviewed table with the wire map — the two things the
same author transcribed — so it could only detect disagreement between them,
never staleness against the page they both transcribe. The reviewed record now
carries per-row provenance, and a drift guard compares three committed
artifacts: the wire map, the reviewed record, and a dated snapshot of the live
catalog. It fails closed on an unexplained delta in either direction, and it is
demonstrated failing on an injected delta rather than only asserted to pass.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| `beam-free` resolves to a surface in the shared registry, `fixed = false` | `rust/crates/eggpool-provider-profile/tests/opencode_go.rs` → `the_models_the_current_table_places_now_resolve` | pass | Chat Completions |
| `minimax-m3.1-flash-free` resolves, `fixed = false` | same test | pass | Chat Completions; placement read from the table, not inferred from `minimax-m3`'s family |
| `step-5-preview` resolves, `fixed = false` | same test | pass | Chat Completions |
| `claude-haiku-5-5` resolves, `fixed = false` | same test | pass | Anthropic Messages |
| Every placed model the table gives an expressible surface has a hint | `reviewed_table_is_covered_exhaustively_by_non_fixed_hints`; asset carries 34 entries against 28 current-table placements | pass | The two-direction assertion still holds and still means "the hints and the reviewed record are the same set" |
| Every hint is `fixed = false` | `every_hint_is_advisory_and_names_a_surface_the_provider_serves`; `operations_o004` | pass | No fixed hint is introduced |
| `gemini-4-argon-contributor` stays unresolved | `the_deliberately_unresolved_models_stay_unresolved` | pass | Recorded with its exact first-party URL |
| The five unplaced catalog ids stay unresolved | same test | pass | `glm-5.1`, `minimax-m2.5`, `omen-alpha`, `qwen3.6-plus`, `qwen3.7-max` |
| Each of the six no-longer-placed entries has a recorded disposition and reason | `every_reviewed_row_records_its_disposition_and_reason` | pass | Nothing removed to make a set comparison tidy |
| A drift guard compares wire map, reviewed record, and dated snapshot | `catalog_drift_guard_passes_on_the_committed_artifacts` | pass | Fails closed on an unexplained delta in either direction |
| The guard is demonstrated failing on an injected delta | `catalog_drift_guard_fails_on_an_injected_unexplained_catalog_id`, `..._surface_disagreement`, `..._missing_withdrawn_record` | pass | Three injection shapes, each asserted to produce a finding |
| A missing or malformed snapshot fails the guard | `catalog_drift_guard_fails_on_a_missing_or_malformed_snapshot` | pass | An absent snapshot never reads as an empty catalog |
| The refresh path is manual, atomic, prints its delta, and is not in CI | `scripts/refresh_opencode_go_catalog_snapshot.py`; `.github/workflows/ci.yml` unchanged | pass | Temp file + one rename; the CI job does not invoke it |
| The crate's dependency list and sans-I/O property are unchanged | `rust/crates/eggpool-provider-profile/tests/boundary.rs` | pass | Still exactly `eggpool-wire`, `serde`, `thiserror`, `toml` |
| No credential, prompt, raw body, or cache key in any artifact | the asset, the fixture, the snapshot, this record | pass | The snapshot carries model ids and a date only; the refresh reads the public catalog without a credential |
| Focused and workspace qualification pass, `--no-default-features` parity preserved | §4 | pass | Local; CI is the same command set |
| Closure records an immutable downstream revision | this record's header | pass | `ffac612` |

## 3. Production implementation evidence

### The canonical asset

`rust/crates/eggpool-provider-profile/assets/_provider_profiles.toml` gains four
`model_wire` entries, each `fixed = false`, each on the surface the current
table gives the model:

```toml
[providers.opencode-go.model_wire."beam-free"]
preferred_surface = "openai_chat_completions"
fixed = false

[providers.opencode-go.model_wire."minimax-m3.1-flash-free"]
preferred_surface = "openai_chat_completions"
fixed = false

[providers.opencode-go.model_wire."step-5-preview"]
preferred_surface = "openai_chat_completions"
fixed = false

[providers.opencode-go.model_wire."claude-haiku-5-5"]
preferred_surface = "anthropic_messages"
fixed = false
```

The asset's header comment now states the 2026-10-22 review, the 29-row table,
and the carried-forward rule. Its `verify` note records that the public
catalog read was re-observed on 2026-10-22 and still proves model availability
only, never credential acceptance. `rust/assets/runtime-manifest.json` carries
the refreshed SHA-256 of the asset, so the runtime-asset lock still holds.

### The reviewed record

`rust/crates/eggpool-provider-profile/tests/opencode_go.rs` is re-transcribed
from the current table. Each of the 34 hinted rows records:

- the exact model id,
- the reviewed wire surface,
- the endpoint URL the current table publishes (`None` for a row the current
  table no longer places, because the table publishes none for it),
- **provenance**: `CurrentTable` or `CarriedForward`,
- **catalog state**: `Listed` or `Withdrawn` at the snapshot date, and
- the reason for that disposition.

Two further sets are recorded separately, because neither can be expressed as a
hint: `UNMODELLED_PLACEMENT` (`gemini-4-argon-contributor`, with its exact
first-party URL) and `UNPLACED_CATALOG_MODELS` (the five catalog ids the table
does not place).

### The catalog snapshot and the drift guard

`rust/crates/eggpool-provider-profile/review/opencode_go_catalog_snapshot.json`
is review evidence, not provider-profile metadata. It lives outside the
canonical asset directory, so `exactly_one_canonical_provider_profile_asset_exists`
keeps its single meaning. It records the 36 model ids the public catalog served
on 2026-10-22, the retrieval date, the source URL, and that no credential was
sent.

`scripts/refresh_opencode_go_catalog_snapshot.py` rewrites it. It reads the
public catalog without a credential, prints the added and removed ids before
anything is written, refuses to write anywhere but the review directory, and
publishes through a temporary file and one rename so two concurrent refreshes
cannot interleave into one snapshot. Committing the result is the human
decision. Nothing in CI invokes it.

The guard compares the three committed artifacts and reports every unexplained
delta:

1. the hints and the reviewed record are the same set, in both directions;
2. a hint's surface agrees with the recorded placement;
3. every catalog id is either hinted or recorded as deliberately unresolved;
4. every hinted model the catalog dropped is recorded as withdrawn, and every
   recorded withdrawal is one the catalog really dropped.

### Runtime projection

`rust/tests/operations_o004.rs` asserts the projected runtime provider carries
every reviewed hint. Its count moved from 30 to 34, which is the additive
change this milestone makes.

## 4. Verification executed

### Commands run

```bash
# formatting
cargo fmt --manifest-path rust/Cargo.toml --all -- --check

# strict lint, default and no-default feature graphs
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings
cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features
cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings

# focused crate gates
cargo test --manifest-path rust/Cargo.toml -p eggpool-provider-profile -- --test-threads=1
cargo test --manifest-path rust/Cargo.toml -p eggpool-provider-profile --no-default-features -- --test-threads=1
cargo build --manifest-path rust/Cargo.toml -p eggpool-provider-profile --no-default-features

# broad workspace gate
cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1

# repository tooling
uv sync --frozen
uv run ruff format --check scripts/ tests/tooling/
uv run ruff check scripts/ tests/tooling/
uv run pyright scripts/
uv run pytest tests/tooling/ -q --tb=short --maxfail=1
uv run python scripts/validate_release_docs.py
uv run python scripts/validate_runtime_package_boundary.py
git diff --check
```

### Results

All commands above are **local** runs on the review workstation. Hosted CI was
not executed for this milestone; the CI job runs the same command set, so the
local results are a faithful proxy for it and are labelled as local rather than
as CI evidence.

| Command | Result |
|---|---|
| `cargo fmt --manifest-path rust/Cargo.toml --all -- --check` | pass — no diff |
| `cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets -- -D warnings` | pass — no warnings |
| `cargo check --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features` | pass |
| `cargo clippy --manifest-path rust/Cargo.toml --workspace --all-targets --no-default-features -- -D warnings` | pass — no warnings |
| `cargo test --manifest-path rust/Cargo.toml -p eggpool-provider-profile -- --test-threads=1` | pass — 37 tests, 0 failed (18 in the OpenCode Go fixture) |
| `cargo build --manifest-path rust/Cargo.toml -p eggpool-provider-profile --no-default-features` | pass |
| `cargo test --manifest-path rust/Cargo.toml -p eggpool-provider-profile --no-default-features -- --test-threads=1` | pass — 37 tests, 0 failed |
| `cargo test --manifest-path rust/Cargo.toml --workspace --all-targets -- --test-threads=1` | pass — 80 targets, **1061 passed, 0 failed, 0 ignored** |
| `uv sync --frozen` | pass |
| `uv run ruff format --check scripts/ tests/tooling/` | pass — 67 files already formatted |
| `uv run ruff check scripts/ tests/tooling/` | pass — all checks passed |
| `uv run pyright scripts/` | pass — 0 errors, 0 warnings, 0 informations |
| `uv run pytest tests/tooling/ -q --tb=short --maxfail=1` | pass — **200 passed, 1 skipped** |
| `uv run python scripts/validate_release_docs.py` | pass — 7 docs checked, version 0.8.2 |
| `uv run python scripts/validate_runtime_package_boundary.py` | pass — Rust runtime, no Python runtime package |
| `git diff --check` | pass — no whitespace errors |

The refresh script was run manually, outside CI and outside the command set
above, to produce the committed snapshot: it printed the 36 catalog ids it read
and a second run printed an empty delta, confirming the refresh is
idempotent.

### Defects found and fixed during verification

Verification was not clean on the first pass. The following were found by the
commands above and fixed before this record was written; none is outstanding.

| Found by | Failure | Fix |
|---|---|---|
| `cargo test --workspace --all-targets` | `rust/tests/coordinator_boundaries.rs::bundled_model_wire_hints_resolve_as_advisory_preferences` asserted 30 published hints | The assertion now expects 34, and the four newly resolvable models were added to its lookup table so the runtime boundary test covers them directly |
| `cargo test -p eggpool-provider-profile` | The fixture's own `every_reviewed_row_records_its_disposition_and_reason` asserted that every carried-forward reason names both review dates | The withdrawn carried-forward reason now names the 2026-10-07 review it carries forward, so the record is self-describing |
| `uv run pytest tests/tooling/` | `tests/tooling/test_installer.py::test_quick_installer_harness_passes` timed out while a concurrent `cargo` build saturated the host | Not a defect. The suite passes in isolation: 200 passed, 1 skipped |

The one skipped tooling test is `tests/tooling/test_release_packaging.py:150`,
an opt-in release-packaging build that is skipped by default and is unrelated to
this milestone.

## 5. Invariant review

| Invariant | Evidence | Status |
|---|---|---|
| The shared crate stays secret-free and sans-I/O | `boundary.rs::crate_has_no_runtime_only_dependency` and `crate_sources_import_no_runtime_or_eggpool_module`; the crate's dependency list is still exactly `eggpool-wire`, `serde`, `thiserror`, `toml` | holds |
| EggPool remains the runtime authority for accounts, credentials, catalog refresh, routing, quota, health, retries, transport, persistence, and wire negotiation | No runtime module moved; the change is embedded metadata plus a test-tree guard | holds |
| `eggpool-wire` remains the authority for wire semantics and codecs | No codec or surface was added; `gemini-4-argon-contributor` was left unresolved rather than mapped onto an existing surface | holds |
| One canonical embedded provider-profile data source | `exactly_one_canonical_provider_profile_asset_exists`; the snapshot is review evidence outside the asset directory and is not parsed by the crate. `rust/crates/eggpool-wire/assets/_wire_profiles.toml` carries a legacy dated hint list, but its `hints()` accessor is not consulted by `rust/src/`, so it is not a second live model→wire source — recorded as a low-severity finding in §10 | holds |
| Existing provider IDs and operator configuration semantics stay compatible | The asset change is additive; `explicit_operator_configuration_overrides_bundled_defaults` still passes | holds |
| Hints are advisory | Every new entry is `fixed = false`; `every_hint_is_advisory_and_names_a_surface_the_provider_serves` | holds |
| Unknown models stay unresolved | `unknown_models_and_unknown_providers_stay_unresolved`; `minimax-m2.5` does not inherit `minimax-m3`'s surface | holds |
| A missing hint and a stale hint are not symmetric | No entry was removed; the six no-longer-placed entries keep their hints with a recorded disposition | holds |
| The crate stays usable by a sibling Rust consumer without the EggPool runtime | `rust/crates/eggpool-provider-profile/consumer-fixture/` still builds and passes | holds |
| No credential, prompt, raw response body, or cache key in any artifact | The snapshot carries model ids and a date; the refresh reads the public catalog with no `Authorization` header; no artifact in this milestone records one | holds |

## 6. Failure and recovery review

- **Malformed or missing catalog snapshot.** A hard failure of the guard, never
  an empty set. `committed_catalog_snapshot` panics on an unreadable file and
  `parse_catalog_snapshot` rejects malformed or partial documents;
  `catalog_drift_guard_fails_on_a_missing_or_malformed_snapshot` asserts it.
- **Missing or malformed canonical asset.** Unchanged: the embedded asset is
  parsed and validated at registry construction.
- **A model that is neither hinted, nor recorded unresolved, nor recorded
  withdrawn.** A guard failure, not a warning. The guard is the only mechanism
  that makes the reviewed record trustworthy, so it fails closed.
- **Concurrent snapshot refreshes.** Each run writes its own temporary file in
  the review directory and publishes with a single `os.replace`, so the
  published snapshot is always one complete document written by one run.
- **A refresh that introduces drift.** The script prints the added and removed
  ids before writing, and it never edits the canonical asset or the reviewed
  record. A reviewer sees the delta before it is committed; the guard then
  fails until the reviewed record is updated to explain it.
- **Cancellation.** The refresh is a bounded synchronous read with an explicit
  timeout; the guard reads committed files only. There is no background task
  and no runtime surface to cancel.

## 7. Migration and compatibility review

- The asset change is **additive**: four new `model_wire` entries with
  `fixed = false`. No existing entry's surface changed, so no consumer's
  dispatch changed.
- A consumer that previously failed closed on `beam-free` or
  `minimax-m3.1-flash-free` now resolves. That is the intended effect and the
  reason the milestone exists.
- The reviewed-record change alters no runtime behavior. It alters the evidence
  record and the drift guard's inputs; the two-direction equality assertion
  keeps its meaning because the carried-forward rows are part of the record,
  not an exemption from it.
- The snapshot and the refresh script add **no runtime dependency** to the
  shared crate: the crate's dependency list is unchanged and its sources still
  import no runtime module. The snapshot is read by the test tree only.
- There is no storage migration. The shared profile asset is embedded metadata,
  not a persisted row format, and the catalog snapshot is review evidence, not
  runtime state.
- Endpoint URL metadata is unchanged, per the plan's §3.7. The first-party
  table publishes per-family URLs under
  `https://opencode.ai/inference/go/{openai,anthropic,google}/v1/...` while the
  profile keeps `https://opencode.ai/zen/go/v1`. Both families were probed
  live on 2026-10-22 (`GET` on the models path answered `200`; an empty `POST`
  on each inference path answered `400`, which distinguishes a live route from
  a `404`), so the profile's base URL is still serving Go models and no
  separate finding arises.

## 8. Security review

- **Auth enforcement.** Unchanged. Per-surface authentication stays Bearer for
  Chat Completions and Responses and `x-api-key` for Anthropic Messages;
  `per_surface_authentication_matches_first_party_documentation` still passes.
- **Secret handling.** No credential, prompt, raw response body, or cache key
  enters the asset, the fixture, the snapshot, the plan, or this record. The
  refresh script attaches no `Authorization` header and reads only the public
  catalog; the snapshot records `credential_used: false`.
- **Redaction.** The snapshot stores model ids and a date. It does not store the
  catalog's raw body, pricing, or any per-model metadata beyond the ids.
- **Privilege bounds.** Unchanged. Nothing here reaches a runtime path.
- **DoS bounds.** The refresh performs one bounded read with a 30-second
  timeout. The guard reads two committed files.
- **Audit behavior.** The script prints the delta it introduces, so the change
  is reviewable before it is committed.
- **Tooling trust.** The refresh script resolves its TLS trust from the
  platform certificate authorities and identifies itself with its own
  user agent, because the upstream edge rejects the default `Python-urllib`
  agent. It reads no environment credential.

## 9. Documentation and operations

- `architecture/deep-dive-providers.md` — "Model-to-wire hints" now states the
  2026-10-22 reviewed date, the three rules (an unplaced catalog model is
  unresolved, a withdrawn model keeps its hint, a surface the contract does
  not model stays unresolved), and the drift guard's fail-closed behavior. The
  review-authority paragraph records that the endpoint URL metadata was
  deliberately left alone and why. "Discovery is not credential proof" records
  the 2026-10-22 re-observation.
- `plans/subsystems/shared-provider-profile-contract-roadmap.md` — M002 status,
  the milestone status table, and the roadmap's completion status.
- `plans/registry.md` — the subsystem row, the removal of the M002 row from
  the dependency-ready table, the recently-closed row, and the closure
  unblock audit.
- The consumer fixture needed no change: it asserts no reviewed set, only
  resolution behavior. It still builds and passes.
- `AGENTS.md` needed no change. No reusable crate gained a durable capability
  other consumers need; the registry is the control surface.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| low | `gemini-4-argon-contributor` remains unhinted | A consumer selecting it still fails closed at `wire_unresolved` | Deliberate. Answering it requires a Google Generative AI surface and codec in `eggpool-wire` — an ADR and a separate milestone, not a metadata edit |
| low | `longcat-2.5-preview-free` stays withdrawn upstream | The route is unavailable; its hint is retained but cannot make the model answer | Upstream's to restore. The upstream answered HTTP 402 to a credentialed request |
| low | `verify.probe_model` is `gpt-5.5-mini`, which the current table and catalog do not list | A verification probe against that id would not resolve a model | Out of scope for M002, which changes only the model-to-surface mapping. `probe_model` is a legacy probe hint that asserts no outcome. Worth a separate bounded review |
| low | `rust/crates/eggpool-wire/assets/_wire_profiles.toml` carries a second, dated `2026-09-02` hint list of seven OpenCode Go entries, including `minimax-m2.5` → `anthropic_messages` | None at runtime, but the two lists disagree | The wire registry is the codec/surface-definition owner; its `hints()` accessor is not consulted by `rust/src/` and is not the model→wire authority, so the canonical asset remains the single live source. The stale entry contradicts the reviewed record, which deliberately leaves `minimax-m2.5` unresolved. Removing or re-syncing it is a separate bounded decision for the wire-kernel owner, not a metadata edit in this milestone |
| low | The reviewed record is a hand-maintained transcription | A future upstream change is still detected by the guard rather than prevented | The guard makes the drift loud; it does not make the table self-updating |

No critical, high, or medium findings remain open for this milestone.

## 11. Roadmap disposition

Milestone closed; the next dependency may proceed. The Shared Provider Profile
Contract roadmap reaches its stated completion definition — M001 and M002 both
have accepted closure records, EggPool consumes the shared contract without a
second live metadata copy, the OpenCode Go profile carries reviewed non-fixed
model-wire hints that agree with the current first-party endpoint table, the
drift is guarded rather than silent, and an immutable revision is available to
CodeGG — and is marked closed.

## 12. Registry updates

Applied in the same commit as this record:

- `plans/registry.md` — the Shared Provider Profile Contract subsystem row moves
  to `closed` with M002's disposition; the M002 row is removed from
  "Dependency-ready implementation plans"; a M002 row is added to "Recently
  closed"; the closure unblock audit is recorded.
- `plans/subsystems/shared-provider-profile-contract-roadmap.md` — M002 status
  moves to `implemented (closed)`, the milestone status table records the
  closure, and the roadmap status moves to `closed`.

Unblock audit: M002 was the only registered successor in this subsystem and is
now closed. No plan in this subsystem is promoted, the blocked-work table has
no entry for it, and no other registered plan depends on it — the only other
ready plan, Persistence M011, is unrelated. CodeGG consumption of M001 remains
unblocked at immutable revision `9ac6a131`; CodeGG consumption of M002 is
pinned to `ffac612`.

## 13. The reviewed table, per-row provenance, and the full delta

### 13.1 Evidence source and review date

First-party endpoint table: `https://opencode.ai/docs/go` (canonical page
`https://opencode.ai/v2/docs/console/go/`), re-reviewed 2026-10-22. The page's
"Endpoints" section publishes 29 rows. The live catalog
`GET https://opencode.ai/zen/go/v1/models` answered `200` without a credential
and returned 36 model rows on the same date.

### 13.2 Per-row provenance

- `CurrentTable` (28 rows): placed by the 2026-10-22 table. Four of them —
  `claude-haiku-5-5`, `minimax-m3.1-flash-free`, `step-5-preview`, `beam-free`
  — were not documented by the 2026-10-07 review and are added by this
  milestone.
- `CarriedForward` (6 rows): the current table no longer places them; the
  placement is carried forward from the 2026-10-07 review. Three
  (`grok-4.6`, `kimi-k2.6`, `minimax-m2.7`) are still in the live catalog, so
  their placement has not been superseded and dropping the hint would break a
  consumer. Three (`deepseek-v4-flash`, `deepseek-v4-flash-vision-exp`,
  `space-bunny`) are absent from both the table and the catalog; the hint is
  retained because a stale hint costs a consumer nothing.

### 13.3 Full delta — current table, live catalog, wire map, before and after

| Model id | Current table (2026-10-22) | Live catalog | Wire map before | Wire map after | Disposition |
|---|---|---|---|---|---|
| `grok-4.7` | Responses | yes | responses | responses | unchanged |
| `grok-4.6` | — | yes | responses | responses | carried forward from 2026-10-07; still catalogued |
| `gpt-6-luna` | Responses | yes | responses | responses | unchanged |
| `gpt-5.6-luna` | Responses | yes | responses | responses | unchanged |
| `muse-spark-1.3-contributor` | Responses | yes | responses | responses | unchanged |
| `muse-spark-1.2-contributor` | Responses | yes | responses | responses | unchanged |
| `claude-haiku-5-5` | Messages | yes | — | messages | **added** |
| `minimax-m3` | Messages | yes | messages | messages | unchanged |
| `minimax-m2.7` | — | yes | messages | messages | carried forward from 2026-10-07; still catalogued |
| `qwen3.8-max` | Messages | yes | messages | messages | unchanged |
| `qwen3.8-flash` | Messages | yes | messages | messages | unchanged |
| `qwen3.7-plus` | Messages | yes | messages | messages | unchanged |
| `gemini-4-argon-contributor` | Google Generative AI | yes | — | — | deliberately unresolved; surface family not modelled |
| `glm-5.3-flash` | Chat Completions | yes | chat | chat | unchanged |
| `glm-5.3` | Chat Completions | yes | chat | chat | unchanged |
| `glm-5.2` | Chat Completions | yes | chat | chat | unchanged |
| `kimi-k3` | Chat Completions | yes | chat | chat | unchanged |
| `kimi-k2.7-code` | Chat Completions | yes | chat | chat | unchanged |
| `kimi-k2.6` | — | yes | chat | chat | carried forward from 2026-10-07; still catalogued |
| `longcat-2.0` | Chat Completions | yes | chat | chat | unchanged |
| `longcat-2.5-preview-free` | Chat Completions | no | chat | chat | unchanged; upstream withdrawn, hint retained |
| `deepseek-v4.1-flash` | Chat Completions | yes | chat | chat | unchanged |
| `deepseek-v4-pro` | Chat Completions | yes | chat | chat | unchanged |
| `deepseek-v4-flash` | — | no | chat | chat | carried forward from 2026-10-07; withdrawn, hint retained |
| `deepseek-v4-flash-vision-exp` | — | no | chat | chat | carried forward from 2026-10-07; withdrawn, hint retained |
| `mimo-v2.6-flash` | Chat Completions | yes | chat | chat | unchanged |
| `mimo-v2.6-pro` | Chat Completions | yes | chat | chat | unchanged |
| `mimo-v2.5` | Chat Completions | yes | chat | chat | unchanged |
| `mimo-v2.5-pro` | Chat Completions | yes | chat | chat | unchanged |
| `hy4-preview` | Chat Completions | yes | chat | chat | unchanged |
| `hy3` | Chat Completions | yes | chat | chat | unchanged |
| `minimax-m3.1-flash-free` | Chat Completions | yes | — | chat | **added** |
| `step-5-preview` | Chat Completions | yes | — | chat | **added** |
| `beam-free` | Chat Completions | yes | — | chat | **added** |
| `space-bunny` | — | no | chat | chat | carried forward from 2026-10-07; withdrawn, hint retained |
| `glm-5.1` | — | yes | — | — | catalogued, not placed; unresolved |
| `minimax-m2.5` | — | yes | — | — | catalogued, not placed; unresolved |
| `omen-alpha` | — | yes | — | — | catalogued, not placed; unresolved |
| `qwen3.6-plus` | — | yes | — | — | catalogued, not placed; unresolved |
| `qwen3.7-max` | — | yes | — | — | catalogued, not placed; unresolved |

Totals: wire map 30 → 34 hints. Reviewed record 34 hinted rows plus 1
deliberately unresolved placement plus 5 deliberately unresolved catalog ids.
Live catalog 36 ids: 30 hinted, 1 unmodelled placement, 5 unplaced.

### 13.4 Placement evidence for the four added hints

Each placement is read directly from the 2026-10-22 table's "Endpoints"
section, whose rows carry the model id, the endpoint, and the SDK package:

| Model id | First-party endpoint row | Placement | Evidence |
|---|---|---|---|
| `beam-free` | `https://opencode.ai/inference/go/openai/v1/chat/completions`, `@ai-sdk/openai-compatible` | `openai_chat_completions` | the table's own row |
| `minimax-m3.1-flash-free` | `https://opencode.ai/inference/go/openai/v1/chat/completions`, `@ai-sdk/openai-compatible` | `openai_chat_completions` | the table's own row |
| `step-5-preview` | `https://opencode.ai/inference/go/openai/v1/chat/completions`, `@ai-sdk/openai-compatible` | `openai_chat_completions` | the table's own row |
| `claude-haiku-5-5` | `https://opencode.ai/inference/go/anthropic/v1/messages`, `@ai-sdk/anthropic` | `anthropic_messages` | the table's own row |

**`minimax-m3.1-flash-free` was placed from the table, not from a family-name
inference.** The table lists `MiniMax M3.1 Flash Free` as its own row on the
OpenAI-compatible chat-completions endpoint. `minimax-m3` sits on the Anthropic
Messages family, and the two ids were not treated as a family: the fixture
records `minimax-m3.1-flash-free` on `openai_chat_completions` and
`minimax-m3` on `anthropic_messages`, and the drift guard would report the
disagreement if either were changed to match the other.

### 13.5 The two deliberately-unresolved sets

`gemini-4-argon-contributor` — placed by the current table on
`https://opencode.ai/inference/go/google/v1beta/models/gemini-4-argon-contributor`,
a Google Generative AI surface. The shared contract models three surfaces for
this provider and `eggpool-wire` owns their codecs. There is no correct way to
represent this model in the current contract, and mapping it onto an existing
surface would be a **wrong** hint: a consumer would dispatch a Google-native
model to an OpenAI-compatible endpoint. Answering it would change
`eggpool-wire`'s surface vocabulary and codec ownership, which the plan
requires to stop for and write an ADR instead. It stays unresolved.

`glm-5.1`, `minimax-m2.5`, `omen-alpha`, `qwen3.6-plus`, `qwen3.7-max` — in the
live catalog and in no first-party placement. A catalog entry proves a model
exists; it does not say which endpoint family serves it. They stay unresolved.

### 13.6 The snapshot

`rust/crates/eggpool-provider-profile/review/opencode_go_catalog_snapshot.json`,
retrieved 2026-10-22 from `https://opencode.ai/zen/go/v1/models` with no
credential, 36 ids:

`beam-free`, `claude-haiku-5-5`, `deepseek-v4-pro`, `deepseek-v4.1-flash`,
`gemini-4-argon-contributor`, `glm-5.1`, `glm-5.2`, `glm-5.3`, `glm-5.3-flash`,
`gpt-5.6-luna`, `gpt-6-luna`, `grok-4.6`, `grok-4.7`, `hy3`, `hy4-preview`,
`kimi-k2.6`, `kimi-k2.7-code`, `kimi-k3`, `longcat-2.0`, `mimo-v2.5`,
`mimo-v2.5-pro`, `mimo-v2.6-flash`, `mimo-v2.6-pro`, `minimax-m2.5`,
`minimax-m2.7`, `minimax-m3`, `minimax-m3.1-flash-free`,
`muse-spark-1.2-contributor`, `muse-spark-1.3-contributor`, `omen-alpha`,
`qwen3.6-plus`, `qwen3.7-max`, `qwen3.7-plus`, `qwen3.8-flash`, `qwen3.8-max`,
`step-5-preview`

### 13.7 Drift guard result, including the demonstrated failure

The guard passes on the committed artifacts: the wire map, the reviewed record,
and the snapshot agree, and `catalog_drift_guard_passes_on_the_committed_artifacts`
asserts an empty finding list.

It was then shown to fail, not merely asserted to be capable of failing. Three
injected deltas each produce a finding:

1. **A snapshot id with no hint and no recorded disposition.** Injecting
   `injected-drift-model` into the snapshot produces the finding "catalog model
   `injected-drift-model` has no hint and no recorded unresolved entry".
2. **A hint's surface disagreeing with the recorded placement.** Moving
   `beam-free`'s hint to `anthropic_messages` produces the finding "hint
   `beam-free` names `anthropic_messages` but the record places it on
   `openai_chat_completions`".
3. **A withdrawal that is not recorded, and a recorded withdrawal the catalog
   has reversed.** Dropping `beam-free` from the snapshot produces "reviewed
   model `beam-free` is absent from the catalog snapshot but is not recorded as
   withdrawn"; re-adding `longcat-2.5-preview-free` produces "reviewed model
   `longcat-2.5-preview-free` is recorded as withdrawn but the catalog still
   lists it".

A fourth injection covers the snapshot itself: a malformed, partial, or absent
snapshot is rejected by `parse_catalog_snapshot` rather than read as an empty
catalog.

## 14. Downstream unblock audit

The registered downstream consumer is CodeGG, which pins an immutable revision
and resolves `opencode-go` routes from this contract. Its recorded finding was
that `beam-free` and `minimax-m3.1-flash-free` are served and free but unmapped
in the shared profile, so it failed them closed at `wire_unresolved`, and that
`longcat-2.5-preview-free` is withdrawn upstream and answers HTTP 402.

M002 closes the first half. `beam-free` and `minimax-m3.1-flash-free` now
resolve on `openai_chat_completions`, and `step-5-preview` and
`claude-haiku-5-5` resolve alongside them.

It cannot close the second half, and no evidence in this repository can: only
the upstream can make `longcat-2.5-preview-free` available again. Its hint is
retained, because a hint for an unavailable model is harmless rather than
harmful, and the route is recorded as withdrawn in the reviewed record so the
drift guard explains it.

CodeGG consumption of M001 remains unblocked at immutable revision `9ac6a131`.
CodeGG consumption of M002 is pinned to `ffac612`.
