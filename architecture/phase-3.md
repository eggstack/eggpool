# Phase 3: Non-Streaming Transparent Proxy

## Overview

Request authentication, header filtering, model/protocol validation, and transparent proxying for both OpenAI-compatible and Anthropic-compatible endpoints.

## Components

### Authentication (`auth.py`)
- Bearer token validation via `Authorization` header
- Constant-time comparison via `hmac.compare_digest`
- Environment variable for proxy API key
- FastAPI dependency for route protection

### Header Processing
- Remove local `Authorization` header
- Insert selected account's upstream credential
- Strip hop-by-hop headers (connection, keep-alive, transfer-encoding, etc.)
- Recalculate or omit `Content-Length`
- Add internal `x-proxy-request-id` header

### Protocol Handlers
- **OpenAI-compatible**: `POST /v1/chat/completions`
- **Anthropic-compatible**: `POST /v1/messages`
- Preserve unknown fields in request/response
- Pass through provider-specific headers

### Request Flow
```
Client Request
    │
    ├── Validate local API key
    ├── Parse model + stream flag
    ├── Resolve protocol
    ├── Select account (routing)
    ├── Create reservation
    ├── Filter headers
    ├── Forward to upstream
    ├── Receive response
    ├── Update usage accounting
    └── Return to client
```

## Key Decisions

1. **Passthrough design**: Unknown fields preserved for protocol evolution
2. **Minimal mutation**: Only add `stream_options.include_usage` when safe
3. **Protocol preservation**: Don't translate between OpenAI/Anthropic protocols
4. **No prompt logging**: Request/response content never persisted
