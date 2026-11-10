# Retrieval Interoperability Milestone 002 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/retrieval-interoperability/002-withdraw-external-retrieval-scope.md`

Source subsystem roadmap:

- `plans/subsystems/retrieval-interoperability-roadmap.md#r002--withdraw-external-retrieval-scope`

Repository baseline reviewed: `e049025274a397d7799c3ae21ed106947c2a1572`

Implementation commits:

- Uncommitted working tree on that baseline. No commit has been created.
  This milestone is documentation and planning links.

## 1. Executive finding

R002 is complete. `docs/retrieval-interoperability.md` states the ADR-0009
contract: embeddings are `POST /v1/embeddings`; document ingest, indexes,
citations, and access checks belong to the caller; PrivateGPT is not a
supported EggPool integration; EggUI owns chat-document grounding. R001 is
not an execution handoff. Embeddings source files are unchanged by this
milestone. The serial Rust suite was not run as R002 evidence.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Current contract page | `docs/retrieval-interoperability.md` | pass | No PrivateGPT ingest steps and no `documents.search` contract. |
| Historical base64 fixture stays historical | Same page, one paragraph | pass | It is not a retrieval claim. |
| R001 plan and blocked closure remain | Both files still exist. Plan status line is `superseded` | pass | Closure text was not rewritten. |
| Registry blocked table has no R001 row | `plans/registry.md` blocked table is empty of R001 | pass | Already true at registration. This closure does not put it back. |
| Embedding roadmap points here | E002 row in `plans/subsystems/embedding-routing-roadmap.md` | pass | |
| Embeddings code unchanged by R002 | `git diff --stat` for embeddings sources is empty | pass | `rust/` is nonempty because M001–M003 landed in the same working tree. Those edits are not this milestone. |
| No new document, chunk, or vector table | No migration in the working tree | pass | |

## 3. Production implementation evidence

None. No Rust, schema, or config change belongs to this milestone.

`docs/deployment.md` and `docs/agent-configuration.md` no longer describe a
PrivateGPT-style recipe. They point at the retrieval page.
`docs/api-reference.md` does not claim EggPool performs retrieval.

## 4. Verification executed

### Commands run

```bash
git diff --check
uv run python scripts/validate_release_docs.py
```

The serial Rust suite was not run for this milestone. It ran for
M001–M003. This record does not claim that suite.

### Results

- `git diff --check`: exit 0.
- `validate_release_docs.py`: `status: pass`, version `0.8.3`, targets `linux-aarch64`, `linux-x86_64`, `macos-arm64`. The script covers release docs, including `docs/deployment.md`, which this milestone's wording touch reached. Local, not CI.

## 5. Invariant review

Embeddings admission, base64 float32 responses, and budget checks are
unchanged by this milestone. The blocked R001 closure stays in place.

## 6. Failure and recovery review

No runtime behavior. Nothing to cancel.

## 7. Migration and compatibility review

The embeddings HTTP contract is unchanged. Operators who followed the old
PrivateGPT page no longer have a supported recipe. The page says that and
points at `POST /v1/embeddings` and `POST /v1/chat/completions`.

## 8. Security review

No new store for documents, chunks, or vectors. No credential was added
to the page.

## 9. Documentation and operations

Rewritten: `docs/retrieval-interoperability.md`. Adjusted pointers:
`docs/deployment.md`, `docs/agent-configuration.md`. Roadmap status row
for R002 and the R001 superseded line were already in the planning change.
The embedding roadmap E002 note already points here.

## 10. Unresolved findings

| Severity | Finding | Impact | Required action |
|---|---|---|---|
| low | Working tree is uncommitted | Closure SHAs are not yet a commit | Commit when requested. |

## 11. Roadmap disposition

Milestone closed. The retrieval workstream's completion definition is met.
R001 stays historical. No application-level RAG capability is claimed.

## 12. Registry updates

R002 leaves the dependency-ready table. The retrieval roadmap is closed.
The blocked table stays empty of R001. Nothing else was waiting on R001,
and no successor is promoted.
