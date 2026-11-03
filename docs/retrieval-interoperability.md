# External retrieval interoperability

EggPool does not ingest documents, store chunks, maintain vector indexes, or
serve private-document search. Retrieval applications own their users,
documents, index, citations, and source access checks. EggPool can provide
model inference and, when explicitly configured, a separately qualified
embeddings endpoint.

## PrivateGPT solo reference topology

PrivateGPT 1.0.1 is the pinned reference target for the current compatibility
instructions. Its package documentation requires Python 3.11 and documents
`OPENAI_API_BASE` for the OpenAI-compatible LLM endpoint and
`OPENAI_EMBEDDING_API_BASE` to override the embedding endpoint. Its API
documentation uses `POST /v1/embeddings` with a registered model ID, and its
release notes identify v1.0.1 as the current release used here. See the
[PrivateGPT package installation guide](https://docs.privategpt.dev/installation/package),
[embeddings API](https://docs.privategpt.dev/api-guide/embeddings), and
[v1.0.1 release](https://github.com/zylon-ai/private-gpt/releases/tag/v1.0.1).

For a single local owner, configure the PrivateGPT instance to send both chat
and embeddings to an EggPool deployment where the selected user token is
authorized for those exact models. Example environment (use a shell session
that is not recorded in logs):

```sh
export OPENAI_API_BASE=http://127.0.0.1:11300/v1
export OPENAI_API_KEY="$EGGPOOL_AGENT_TOKEN"
export OPENAI_EMBEDDING_API_BASE=http://127.0.0.1:11300/v1
export PGPT_HOME="$HOME/.local/share/private-gpt-eggpool-reference"
private-gpt serve --port 8080
```

Use a PrivateGPT 1.0.1 model profile whose chat model ID is visible through
EggPool `/v1/models` and whose embedding model ID is explicitly configured in
EggPool's `embedding_models` allowlist. The chat and embedding upstreams may be
separate EggPool providers, but each pair must independently pass its wire
qualification. Keep the token in a private environment manager; do not put it
in checked-in TOML, container files, command transcripts, or sample logs.

To qualify an installation, use a small public fixture document containing a
unique fact, ingest it through the PrivateGPT version's own documented ingest
API, ask a question whose answer is in that fixture, then inspect the returned
source citation and resolve it to the same PrivateGPT-owned document revision.
Repeat with an unrelated question to confirm that the answer does not cite the
fixture. PrivateGPT 1.0.1 was started against EggPool with Ollama 0.17.7;
direct model discovery and embedding calls succeeded, but the documented
`/v1/artifacts/ingest` call failed when EggPool rejected PrivateGPT's embedding
request with `Request could not be admitted`. No retrieval or citation check
was reached, and the request/admission discrepancy is unresolved. The topology
above remains an operator reference, not a claim that EggPool provides
document ACLs or citations.

## Managed users and MCP retrieval

The solo example is not a team isolation design. One shared PrivateGPT process
and one EggPool token would make its EggPool model privileges common to every
PrivateGPT caller. It must not be used as a substitute for user-specific
document authorization.

EggPool's current MCP gateway is fixed to its vetted Eggsearch tool catalog.
It does not register arbitrary retrieval adapters. A team deployment needs a
separately qualified adapter that authenticates the caller through signed or
scope-limited delegation and enforces document ACLs on both search and fetch.
It must reject spoofed principal headers, foreign source IDs, revoked users,
stale revisions, and an absent delegation token. No shared administrator key
may stand in for that contract. Until those backend controls and two-principal
negative tests exist, managed-team private retrieval remains unsupported.

Retrieved document text remains untrusted model input. Citations must carry
backend-owned source identity and revision information; EggPool cannot verify
or repair citations because it does not own the retrieval store. Logs and
metrics must not contain queries, document text, source URLs, or credentials.

