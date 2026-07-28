"""Shared test fixtures and configuration."""

import os
import sys

import pytest

pytestmark = [pytest.mark.asyncio]

SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

# Re-export shared real-runtime fixture for smoke and integration tests
from tests.helpers.real_runtime import real_runtime_app as real_runtime_app  # noqa: F401, E402
