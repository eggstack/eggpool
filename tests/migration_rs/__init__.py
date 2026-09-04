"""Black-box compatibility tooling for the Rust migration.

The migration harness is deliberately test-only.  It launches the existing
Python command and the Rust candidate by explicit paths and exchanges only
serializable observations at the comparison boundary.
"""

from tests.migration_rs.harness import (
    ConfigObservation,
    DatabaseObservation,
    HtmlObservation,
    HttpObservation,
    Implementation,
    PythonLauncher,
    RustLauncher,
    StaticObservation,
    StubHttpServer,
    allocate_tcp_port,
    assert_distinct_implementations,
    capture_startup_state,
    compare_observations,
    isolated_environment,
    normalize_observation,
)

__all__ = [
    "ConfigObservation",
    "DatabaseObservation",
    "HtmlObservation",
    "HttpObservation",
    "Implementation",
    "PythonLauncher",
    "RustLauncher",
    "StaticObservation",
    "StubHttpServer",
    "allocate_tcp_port",
    "assert_distinct_implementations",
    "compare_observations",
    "capture_startup_state",
    "isolated_environment",
    "normalize_observation",
]
