"""Shared test fixtures and configuration."""

import os
import sys

import pytest

from eggpool.request.routing_trace_guard import reset_routing_trace_guard
from tests.helpers.real_runtime import (
    real_runtime_app as real_runtime_app,  # noqa: F401
)

pytestmark = [pytest.mark.asyncio]

SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)


@pytest.fixture(autouse=True)
def _reset_process_singletons() -> None:
    """Prevent process-global diagnostic helpers leaking between tests."""
    reset_routing_trace_guard()
