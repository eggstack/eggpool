"""Repeated-run admission race stress test.

Plan acceptance criterion:

> Concurrent admission coverage passes or fails consistently for at
> least 100 repeated runs.

This script runs the concurrent reload admission test in a tight loop
and records the outcomes.  Each iteration:

- Initializes a fresh ``ReloadHarness``
- Launches two concurrent reloads with the second held inside the
  candidate build
- Records whether the second reload was rejected (ReloadInProgressError)
  or both succeeded (TOCTOU race)

Exits 0 if the behavior is consistent (either ``reload_in_progress``
in every iteration, or no rejection at all).  Exits non-zero with a
summary otherwise.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from eggpool.control.reload_manager import (  # noqa: E402
    ReloadInProgressError,
)


async def _run_once() -> str:
    """One iteration of the admission race. Returns outcome label."""
    from tests.support.reload_harness import ReloadHarness

    async with ReloadHarness() as harness:
        # Drop the drain timeout so the test ends fast. The harness's
        # initial generation is retired after the second reload's
        # build fails, but the test only cares about admission, not
        # retirement completion.
        harness.reload_manager._drain_timeout_s = 0.05  # type: ignore[attr-defined]
        preparation_event = asyncio.Event()
        harness.reload_manager.preparation_event = preparation_event

        first_result: object = None
        first_error: Exception | None = None
        second_result: object = None
        second_error: Exception | None = None

        async def do_first() -> None:
            nonlocal first_result, first_error
            try:
                first_result = await harness.reload()
            except Exception as exc:  # noqa: BLE001
                first_error = exc

        async def do_second() -> None:
            nonlocal second_result, second_error
            try:
                second_result = await harness.reload()
            except Exception as exc:  # noqa: BLE001
                second_error = exc

        t1 = asyncio.create_task(do_first())
        await asyncio.sleep(0.05)
        t2 = asyncio.create_task(do_second())
        await asyncio.sleep(0.05)
        preparation_event.set()
        await asyncio.gather(t1, t2, return_exceptions=True)
        harness.reload_manager.preparation_event = None

        if second_error is not None:
            err: Exception = second_error
            if isinstance(err, ReloadInProgressError):
                return "rejected"
        if second_result is not None and first_result is not None:
            return "both_admitted"
        if first_error is not None or second_error is not None:
            return f"unexpected_error:{first_error!r}:{second_error!r}"
        return "unknown"


async def main(iterations: int) -> int:
    outcomes: dict[str, int] = {}
    for i in range(iterations):
        outcome = await _run_once()
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        if (i + 1) % 10 == 0:
            print(f"[{i + 1}/{iterations}] {outcomes}", flush=True)

    print(f"final outcomes after {iterations} runs: {outcomes}", flush=True)

    # Acceptable outcomes:
    # - "rejected" every time (current implementation works correctly
    #   for the GIL-serialized event loop)
    # - "both_admitted" every time (TOCTOU race is reproducible)
    # Mixed outcomes are not acceptable for an acceptance criterion.
    if len(outcomes) == 1 and "rejected" in outcomes:
        print("ACCEPTED: rejected in every iteration", flush=True)
        return 0
    if len(outcomes) == 1 and "both_admitted" in outcomes:
        print(
            "ACCEPTED: TOCTOU race consistently triggered in every iteration",
            flush=True,
        )
        return 0
    print("NOT ACCEPTED: outcomes varied across runs", flush=True)
    return 1


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    rc = asyncio.run(main(n))
    raise SystemExit(rc)
