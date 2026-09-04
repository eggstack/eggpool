# ADR-0002 — Rust Runtime, HTTP Stack, SSR Parity, and Implementation Location

Status: accepted

## Context

The Python server currently combines FastAPI/ASGI, Granian, HTTPX/httpcore, asyncio, Pydantic, aiosqlite, Click, and server-rendered dashboard code. The Rust rewrite should reduce runtime/process complexity without creating a highly fragmented Rust workspace.

The dashboard's visual design is effectively complete; migration requires rendering parity, not a frontend redesign.

## Alternatives considered

HTTP/runtime:

- Reqwest for outbound plus Axum for inbound;
- Hyper/Hyper-util/Rustls for outbound plus Axum/Tower for inbound;
- a custom HTTP stack.

Source layout:

- replace repository root with Cargo immediately;
- many migration crates/workspace members;
- one side-by-side Rust package under `rust/`.

Frontend:

- replace SSR with a client SPA;
- keep Python renderer and call it from Rust;
- port SSR behavior and copy static assets.

## Decision

1. Begin with one non-published Cargo package under `rust/`.
2. Produce an `eggpool` binary at `rust/target/...` but do not install it over Python during migration.
3. Use Tokio as the async runtime.
4. Use Axum/Tower for inbound HTTP and Hyper/Hyper-util + Rustls for outbound HTTP.
5. Do not add Reqwest unless a later ADR demonstrates a separate requirement that Hyper cannot reasonably satisfy.
6. Port server-side dashboard rendering to Rust while preserving route/DOM/content/escaping behavior.
7. Copy the frozen static assets/themes into the Rust packaging boundary and guard them against unintended drift during dual implementation.
8. Prefer modules within the package over many internal crates until a real independent ownership/reuse boundary appears.

## Consequences

The Granian supervisor/worker process model can disappear in the Rust end state. Systemd can supervise the Rust process directly, while the CLI preserves the existing foreground/daemon intent.

Using Hyper directly keeps the outbound transport boundary compatible with custom Eggress streams and avoids stacking two HTTP client libraries.

SSR work becomes a rendering parity project rather than a frontend architecture project.

## Compatibility implications

Granian-specific process names/headers are not automatically preserved. EggPool-owned runtime API fields and operator workflows remain compatibility targets.

Existing dashboard URLs, scripts, static paths, themes, CSS classes/IDs used by JavaScript, escaping behavior, and rendered information remain targets.

## Deferred decisions

Exact HTML rendering helper/template mechanism is intentionally not fixed here. The implementation should choose the simplest Rust mechanism that can preserve output semantics without introducing a frontend framework.
