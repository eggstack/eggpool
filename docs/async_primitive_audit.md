# Async primitive audit

EggPool uses one canonical asyncio event-loop thread. Event-loop-bound locks,
conditions, and queues must be created and consumed within that loop and must
retire with their owning process or runtime generation.

The optional-runtime reduction removed the custom DNS resolver and compression
tuning registry, so they have no locks or task lifetimes to audit. HTTPX and
the operating-system resolver handle ordinary network reuse; per-account proxy
transport remains generation-owned.

When changing a background service, verify construction, cancellation, and
retirement paths with the focused lifecycle tests. Disabled features must
construct no task, queue, recorder, or client.
