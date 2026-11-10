# Retrieval

EggPool does not ingest documents, store chunks, maintain vector indexes,
or serve private-document search. Document ingest, indexes, citations, and
access checks belong to the calling application. PrivateGPT is not a
supported EggPool integration.

Callers that want embeddings use `POST /v1/embeddings`. EggPool does not
store the vectors. Callers that want a model answer use
`POST /v1/chat/completions` or the other inference routes. EggUI owns
chat-document grounding for that application. Another application may
call embeddings without EggPool storing vectors.

A historical base64 float32 embeddings admission fixture remains evidence
that the embeddings route accepts that encoding. It is not a retrieval
claim and it is not a PrivateGPT setup procedure.
