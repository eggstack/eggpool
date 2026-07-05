# Background Task Scheduler API Cleanup Plan

## Context

The background-task overdue remediation is now mostly complete. The scheduler now owns periodic cadence through `TaskSupervisor.register_periodic(...)`, runtime snapshots expose `next_run_at`, `overdue_seconds`, tick counters, and `last_tick_duration_ms`, the dashboard uses supervisor-owned timing fields, metrics shutdown flush runs after supervisor stop, and `MetricsWriteCoalescer.flush()` restores buffered events if cancelled mid-write.

The remaining cleanup is narrow: the public scheduler API still accepts `run_immediately` but discards it, and one zero-delay test has a misleading docstring that does not match actual behavior. This pass should make scheduler first-tick semantics explicit and pinned by tests.

## Current issue

`TaskSupervisor.register_periodic(...)` currently has this public signature:

```python
def register_periodic(
    self,
    name: str,
    tick_factory: Callable[[], Coroutine[Any, Any, None]],
    *,
    interval_s: float,
    run_immediately: bool = False,
    initial_delay_s: float | None = None,
    timeout_s: float | None = None,
    max_restarts: int = 10,
) -> SupervisedTask:
    ...
```

But the implementation does:

```python
del run_immediately, timeout_s  # reserved for future scheduler work
```

`initial_delay_s` now works and is stored as `_initial_delay_s`. A stored value of `0.0` causes the runner to sleep zero seconds before the first tick. That behavior is reasonable, but the test `test_register_periodic_allows_zero_initial_delay()` currently documents `initial_delay_s=0.0` as meaning "use interval_s as initial delay", which is the opposite of the runner's actual behavior.

The API should not silently accept a meaningful-looking argument that has no effect. This cleanup should either implement `run_immediately` or remove/defer it. Prefer implementing it because it is straightforward and aligns with the existing zero-delay behavior.

## Goals

1. Make `run_immediately=True` perform an immediate first tick.
2. Define precedence between `run_immediately` and `initial_delay_s` explicitly.
3. Correct zero-delay test/documentation so it matches actual semantics.
4. Add tests that fail if `run_immediately` is silently ignored again.
5. Preserve current sleep-first behavior for existing callers that omit both `run_immediately` and `initial_delay_s`.
6. Preserve `automatic_backup` startup-delay behavior.
7. Avoid touching unrelated model-info, dashboard cache-page, cost, or quota changes.

## Proposed semantics

Use these first-tick scheduling rules:

1. `interval_s` must be strictly greater than zero.
2. If `run_immediately=True`, the first delay is `0.0`.
3. If `run_immediately=False` and `initial_delay_s is not None`, the first delay is `initial_delay_s`.
4. If `run_immediately=False` and `initial_delay_s is None`, the first delay is `interval_s`.
5. `initial_delay_s` must be greater than or equal to zero if supplied.
6. `run_immediately=True` and `initial_delay_s` should not both be supplied unless the project explicitly wants precedence behavior. Prefer rejecting the combination with `ValueError` so caller intent is unambiguous.
7. After the first tick completes or fails, all subsequent sleeps use `interval_s`.
8. `next_run_at` should be primed from the actual first delay, so pre-start and early runtime dashboard countdowns match real scheduling.

Recommended validation:

```python
if interval_s <= 0:
    raise ValueError(...)
if initial_delay_s is not None and initial_delay_s < 0:
    raise ValueError(...)
if run_immediately and initial_delay_s is not None:
    raise ValueError(
        "Periodic task ... cannot set both run_immediately and initial_delay_s"
    )
first_delay_s = 0.0 if run_immediately else (
    float(interval_s) if initial_delay_s is None else float(initial_delay_s)
)
```

Alternatively, if the project prefers permissive precedence, document that `run_immediately=True` overrides `initial_delay_s`. The strict `ValueError` path is safer because it prevents accidental misconfiguration.

## Implementation steps

1. Update `TaskSupervisor.register_periodic(...)` in `src/eggpool/background/__init__.py`:
   - Stop deleting `run_immediately`.
   - Continue deleting or ignoring `timeout_s` with a docstring note, since timeout enforcement is still future work.
   - Validate `initial_delay_s >= 0` when supplied.
   - Reject `run_immediately=True` plus non-null `initial_delay_s`, unless choosing explicit override semantics.
   - Compute `first_delay_s` using the rules above.
   - Store `_initial_delay_s=first_delay_s` on `SupervisedTask`.
   - Prime `_next_run_at = time.time() + first_delay_s`.

2. Update the `register_periodic(...)` docstring:
   - Replace "Reserved for future use" for `run_immediately` with the real behavior.
   - Keep `timeout_s` as "accepted for forward compatibility; currently unused" or remove it from the signature if no caller uses it. Removing it may be a breaking API change, so retaining it with clear documentation is safer.

3. Update tests in `tests/unit/test_background.py`:
   - Rename or rewrite `test_register_periodic_allows_zero_initial_delay()` so the docstring says `initial_delay_s=0.0` means immediate first tick.
   - Add `test_register_periodic_run_immediately_first_tick()` to verify the first tick fires before `interval_s` elapses.
   - Add `test_register_periodic_run_immediately_primes_next_run_at_now()` or fold this assertion into the first-tick test.
   - Add `test_register_periodic_rejects_run_immediately_with_initial_delay()` if using strict validation.
   - Keep `test_initial_delay_s_none_defaults_to_interval()` unchanged so default sleep-first semantics remain pinned.

4. Inspect app registrations:
   - Confirm no current app registration passes `run_immediately=True`.
   - Confirm `automatic_backup` still passes `initial_delay_s=config.backup.startup_delay_s` and is unaffected.
   - Do not change existing task cadences.

5. Update docs if needed:
   - `architecture/README.md`, `README.md`, or `.opencode/skills/architecture/SKILL.md` if they mention first-delay semantics.
   - Keep docs concise: supervisor-owned periodic tasks sleep first by default, may set `initial_delay_s`, and may set `run_immediately=True` for an immediate first tick.

## Test details

### `test_register_periodic_run_immediately_first_tick`

Suggested pattern:

```python
@pytest.mark.asyncio
async def test_register_periodic_run_immediately_first_tick() -> None:
    tick_count = 0
    tick_times: list[float] = []

    async def tick() -> None:
        nonlocal tick_count
        tick_count += 1
        tick_times.append(time.time())

    supervisor = TaskSupervisor()
    supervisor.register_periodic(
        "immediate",
        tick,
        interval_s=1.0,
        run_immediately=True,
    )

    start = time.time()
    await supervisor.start_all()
    for _ in range(20):
        if tick_count >= 1:
            break
        await asyncio.sleep(0.01)
    await supervisor.stop_all()

    assert tick_count >= 1
    assert tick_times[0] - start < 0.2
```

Use a loose threshold because CI scheduling can be noisy. The important invariant is that the first tick happens well before `interval_s=1.0`.

### `test_register_periodic_rejects_run_immediately_with_initial_delay`

```python
def test_register_periodic_rejects_run_immediately_with_initial_delay() -> None:
    supervisor = TaskSupervisor()

    async def tick() -> None:
        return None

    with pytest.raises(ValueError, match="run_immediately.*initial_delay_s"):
        supervisor.register_periodic(
            "ambiguous",
            tick,
            interval_s=10.0,
            run_immediately=True,
            initial_delay_s=5.0,
        )
```

### Correct zero-delay test

Replace the current misleading docstring with:

```python
def test_register_periodic_allows_zero_initial_delay() -> None:
    """``initial_delay_s=0.0`` schedules the first tick immediately."""
    ...
```

Prefer adding an async behavior assertion rather than only checking the private `_initial_delay_s` field. If keeping a simple registration test, add a separate async test to prove first-tick timing.

## Acceptance criteria

The cleanup is complete when:

1. `run_immediately=True` is no longer silently discarded.
2. `run_immediately=True` schedules the first tick without waiting `interval_s`.
3. Default callers still sleep `interval_s` before the first tick.
4. `initial_delay_s` callers still sleep exactly the configured first delay.
5. `run_immediately=True` plus `initial_delay_s` is either rejected or explicitly documented and tested with deterministic precedence.
6. The zero-delay test docstring matches actual behavior.
7. No app task registration unintentionally changes cadence.
8. Targeted tests pass: `tests/unit/test_background.py` at minimum.
9. Full static/test gate passes if available: `ruff`, `pyright`, and pytest.

## Suggested command sequence

```bash
uv run ruff check src/eggpool/background/__init__.py tests/unit/test_background.py
uv run pyright src/eggpool/background/__init__.py tests/unit/test_background.py
uv run pytest tests/unit/test_background.py -q
```

If those pass, run the broader scheduler-related subset:

```bash
uv run pytest tests/unit/test_background.py tests/unit/test_runtime_metrics.py tests/unit/test_dashboard.py tests/unit/test_metrics_buffer.py -q
```

## Suggested commit message

```text
background: clarify periodic first-tick API

Implement run_immediately for register_periodic, reject ambiguous
initial-delay combinations, and fix zero-delay tests/docs so the public
scheduler API matches actual first-tick behavior.
```
