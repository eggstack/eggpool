"""Observability subsystem — background trace writers and diagnostics.

The :mod:`observability` package owns the process-owned
:class:`~eggpool.observability.routing_trace_writer.RoutingTraceWriter`
and the immutable
:class:`~eggpool.observability.routing_trace_writer.RoutingTraceEvent`
contract used for off-path routing-decision trace persistence.
The optional :class:`~eggpool.observability.outbound.OutboundObservation`
contract is an in-memory, sanitized hook for explicit live diagnostics.

Key invariants:

- Trace writes are never on the synchronous dispatch path.
- The writer is process-owned and survives generation swaps.
- Queue overload or writer failure never delays correctness-critical dispatch.
- All trace loss is classified and surfaced via snapshot counters.
- Outbound observations contain structural facts only; they never retain
  credentials or raw request/response content.
"""
