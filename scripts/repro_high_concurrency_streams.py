"""Reproduce the high-concurrency streaming harness from the CLI.

Drives a configurable burst of streaming requests through the
coordinator pipeline (mock upstream) and prints a structured summary of
runtime telemetry, DB state, and lock contention.  Useful for operators
who want to validate stream stability outside the pytest harness:

    python -m scripts.repro_high_concurrency_streams \\
        --concurrency 100 --cancel-rate 0.25 --cancel-offset 3

The script is intentionally a thin shell over the same primitives the
integration test exercises so the harness and the script stay in sync.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any

import httpx
import respx

from eggpool.accounts.registry import AccountRegistry
from eggpool.catalog.service import CatalogService
from eggpool.db.connection import Database
from eggpool.db.migrations import MigrationRunner
from eggpool.db.repositories import (
    AttemptRepository,
    RequestRepository,
    ReservationRepository,
    UsageWindowRepository,
)
from eggpool.health.health_manager import HealthManager
from eggpool.models.config import AppConfig
from eggpool.request.coordinator import (
    ProxyRequestContext,
    RequestCoordinator,
)
from eggpool.request.stream_diagnostics import (
    STREAM_OUTCOME_CLIENT_CANCELLED,
    STREAM_OUTCOME_COMPLETED,
    get_stream_diagnostics,
)
from eggpool.routing.router import Router

UPSTREAM_BASE = "https://test-upstream.example.com"


def _build_config() -> AppConfig:
    os.environ.setdefault("OPENCODE_TEST_KEY", "test-key-123")
    return AppConfig.from_dict(
        {
            "server": {
                "api_key_env": "OPENCODE_TEST_KEY",
                "host": "127.0.0.1",
                "port": 0,
            },
            "database": {"path": ":memory:"},
            "upstream": {"base_url": UPSTREAM_BASE},
            "models": {"startup_refresh": False, "refresh_interval_s": 0},
            "accounts": [{"name": "test-acct", "api_key_env": "OPENCODE_TEST_KEY"}],
            "dashboard": {"enabled": False},
        }
    )


async def _build_coordinator(
    config: AppConfig,
) -> tuple[RequestCoordinator, Database, httpx.AsyncClient]:
    database = Database(path=":memory:")
    await database.connect()
    runner = MigrationRunner(database)
    await runner.run()
    async with database.transaction():
        await database.execute_write(
            "INSERT INTO accounts (name, api_key_env, enabled, weight) "
            "VALUES (?, ?, 1, 1.0)",
            ("test-acct", "OPENCODE_TEST_KEY"),
        )
        await database.execute_write(
            "INSERT OR IGNORE INTO models (model_id, protocol) VALUES (?, ?)",
            ("gpt-4", "openai"),
        )
    httpx_client = httpx.AsyncClient(
        base_url=config.upstream.base_url,
        timeout=httpx.Timeout(300.0, connect=5.0, read=300.0, write=30.0, pool=30.0),
    )
    registry = AccountRegistry(config)
    catalog = CatalogService(config, registry, database, httpx_client)
    catalog.cache.load_model(
        model_id="gpt-4",
        display_name="GPT-4",
        protocol="openai",
        capabilities={},
        source_metadata={},
    )
    catalog.cache.add_account_support("gpt-4", "test-acct")
    router = Router(registry, catalog)
    router.set_account_weight("test-acct", 1.0)
    coord = RequestCoordinator(
        registry=registry,
        catalog=catalog,
        router=router,
        db=database,
        client_pool=httpx_client,
        request_repo=RequestRepository(database),
        reservation_repo=ReservationRepository(database),
        attempt_repo=AttemptRepository(database),
        usage_window_repo=UsageWindowRepository(database),
        health_manager=HealthManager(),
    )
    return coord, database, httpx_client


def _make_body() -> bytes:
    return json.dumps(
        {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
        }
    ).encode()


def _iter_chunks(count: int, delay: float) -> Any:
    def _gen() -> Any:
        for i in range(count):
            yield (
                f'data: {{"choices":[{{"delta":{{"content":"tok{i}"}}}}]}}\n\n'
            ).encode()
            time.sleep(delay)
        yield (
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
            b'"usage":{"prompt_tokens":1,"completion_tokens":1,'
            b'"total_tokens":2}}\n\n'
        )
        yield b"data: [DONE]\n\n"

    return _gen()


async def _run_burst(
    coord: RequestCoordinator,
    *,
    concurrency: int,
    cancel_rate: float,
    cancel_offset: int,
    chunks_per_stream: int,
    delay: float,
) -> dict[str, Any]:
    diagnostics = get_stream_diagnostics()
    initial_outcomes = dict(diagnostics.snapshot()["outcomes"])

    completed = 0
    cancelled = 0
    failures = 0

    with respx.mock(assert_all_called=False) as mock:

        async def _side_effect(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=_iter_chunks(chunks_per_stream, delay),
            )

        mock.post(f"{UPSTREAM_BASE}/chat/completions").mock(side_effect=_side_effect)

        async def _drive_one(idx: int) -> None:
            nonlocal completed, cancelled, failures
            req_id = f"repro-{idx:04d}"
            cancel = (idx / max(1, concurrency)) < cancel_rate
            ctx = ProxyRequestContext(
                request_id=req_id,
                protocol="openai",
                model_id="gpt-4",
                streaming=True,
                original_body=_make_body(),
                incoming_headers={"content-type": "application/json"},
            )
            chunks_seen = 0
            started = asyncio.Event()
            at_target = asyncio.Event()

            async def _drive() -> None:
                response = await coord.execute(ctx)
                if response.stream_iterator is None:
                    return
                nonlocal chunks_seen
                async for _chunk in response.stream_iterator:
                    chunks_seen += 1
                    if not started.is_set():
                        started.set()
                    if cancel and chunks_seen >= cancel_offset:
                        at_target.set()
                    else:
                        await asyncio.sleep(0)

            task = asyncio.create_task(_drive())
            try:
                if cancel:
                    await asyncio.wait_for(started.wait(), timeout=2.0)
                    await asyncio.wait_for(at_target.wait(), timeout=2.0)
                    await asyncio.sleep(0)
                    task.cancel()
                await task
                completed += 1
            except asyncio.CancelledError:
                cancelled += 1
            except Exception:
                failures += 1
            if cancel:
                await asyncio.sleep(0.3)

        deadline = time.monotonic() + 30.0
        for i in range(concurrency):
            remaining = max(0.01, deadline - time.monotonic())
            try:
                await asyncio.wait_for(_drive_one(i), timeout=remaining)
            except TimeoutError:
                break

    final_outcomes = dict(diagnostics.snapshot()["outcomes"])
    return {
        "concurrency": concurrency,
        "cancel_rate": cancel_rate,
        "completed": completed,
        "cancelled": cancelled,
        "failures": failures,
        "outcomes_delta": {
            STREAM_OUTCOME_COMPLETED: (
                final_outcomes.get(STREAM_OUTCOME_COMPLETED, 0)
                - initial_outcomes.get(STREAM_OUTCOME_COMPLETED, 0)
            ),
            STREAM_OUTCOME_CLIENT_CANCELLED: (
                final_outcomes.get(STREAM_OUTCOME_CLIENT_CANCELLED, 0)
                - initial_outcomes.get(STREAM_OUTCOME_CLIENT_CANCELLED, 0)
            ),
        },
    }


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--cancel-rate", type=float, default=0.0)
    parser.add_argument("--cancel-offset", type=int, default=2)
    parser.add_argument("--chunks-per-stream", type=int, default=6)
    parser.add_argument("--chunk-delay-s", type=float, default=0.01)
    args = parser.parse_args()

    config = _build_config()
    coord, db, httpx_client = await _build_coordinator(config)
    try:
        summary = await _run_burst(
            coord,
            concurrency=args.concurrency,
            cancel_rate=args.cancel_rate,
            cancel_offset=args.cancel_offset,
            chunks_per_stream=args.chunks_per_stream,
            delay=args.chunk_delay_s,
        )
    finally:
        await httpx_client.aclose()
        await db.disconnect()

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
