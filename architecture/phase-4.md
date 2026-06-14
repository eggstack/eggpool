# Phase 4: Streaming Proxy

## Overview

Byte-preserving streaming relay with SSE observation, usage extraction, and cancellation propagation.

## Components

### Streaming Pipeline (`proxy/streaming.py`)
- Forward chunks without whole-response buffering
- Preserve byte order and SSE delimiters
- Measure time to first byte
- Detect downstream cancellation
- Parse stream copy for usage/errors

### SSE Observer
- Buffer only incomplete SSE frames
- Split events on blank-line boundaries
- Parse `event:` and `data:` fields
- Enforce maximum frame size
- Record malformed frames without interrupting forwarding

### Usage Extraction (`proxy/usage.py`)
- **OpenAI**: Extract usage from `stream_options` response
- **Anthropic**: Extract usage from `message_delta` and `message_stop` events
- Parse token counts and cost data from stream

## Stream Completion Categories

- `completed` — Normal completion with usage data
- `upstream_error_before_body` — Error before any body bytes
- `upstream_error_midstream` — Error during streaming
- `client_cancelled` — Downstream disconnect
- `proxy_cancelled` — Proxy-initiated cancellation
- `timeout` — Read timeout exceeded
- `malformed_terminal_usage` — Invalid usage data at stream end

## Key Decisions

1. **Byte preservation**: No decode/re-encode of arbitrary payload
2. **No post-first-byte retry**: Prevents duplicate output
3. **Bounded memory**: Stream processing uses fixed buffers
4. **Async usage persistence**: DB writes don't block stream forwarding
