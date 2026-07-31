# Dispatch Stability Runbook

Dispatch stability is diagnosed with the runtime telemetry already exposed by
EggPool, plus focused request-path tests. There is no mandatory soak runner or
JSON evidence format.

## Quick checks

```bash
eggpool runtime-status --json | python3 -m json.tool
uv run pytest tests/smoke/ -q --tb=short --maxfail=1
```

Compare `local_pre_upstream` with upstream connect/TTFT to separate EggPool
work from provider latency. Inspect `stream_diagnostics` for provider-bound
connect, read, write, protocol, and timeout outcomes. After a failure or
cancellation, verify that active requests and reservations return to zero.

For a stream-specific issue, use the bounded reproducer:

```bash
uv run python scripts/repro_high_concurrency_streams.py --help
```

Run the focused tests for the changed ownership boundary. In particular,
streaming tests distinguish canonical terminal evidence (`[DONE]` or
`message_stop`) from premature EOF; a timeout is a separate upstream outcome.
Runtime metrics are operational signals and should not be converted into
fixed CI percentile or duration gates.

See [releasing.md](../releasing.md) for the optional target-device smoke and
[architecture/README.md](../../architecture/README.md) for lifecycle details.
