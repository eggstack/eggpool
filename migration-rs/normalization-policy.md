# F002 Observation Normalization Policy

Status: accepted for the F002 harness

The differential harness compares observations only after the two launchers
have passed the distinct-implementation guard.  Normalization is an adapter
at the observation boundary, not a recursive scrubber.  The raw observation
retains the implementation identity, executable, PID, and duration for failure
diagnostics; persisted seed captures use the normalized form.

## Allowed rules

| Observation | Field | Normalization | Reason | Regression coverage |
|---|---|---|---|---|
| All process observations | `implementation`, `executable` during comparison | Remove from the comparison projection | The launchers already prove the implementations differ; executable paths are not product output | `test_distinct_implementation_guard_and_real_process_identity` |
| CLI/config process | `args` path values, stdout, stderr | Replace only an explicitly supplied temporary root with `<TEMP_ROOT>` | Isolated config/data/state paths are intentionally unique per run | `test_normalization_removes_only_explicit_ephemeral_fields` |
| CLI/config process | `pid`, `duration_ms` | Omit from comparison projection | Scheduling and process allocation are incidental | `test_normalization_removes_only_explicit_ephemeral_fields` |
| HTTP | response headers | Capture only `cache-control`, `content-type`, `allow`, and `www-authenticate`; sort names | `Date`, `Server`, `Content-Length`, and framework headers are transport/framework details | `test_stub_http_drains_without_persisting_request_body` |
| HTTP JSON | `body` | Parse and re-emit with sorted keys and compact separators | Object member order is not JSON semantics; unknown fields remain visible | `test_contractual_json_field_change_fails_comparison` |
| HTTP/SSE | `sse_frames` | Preserve event, id, and every data line in order | SSE grammar and frame order are contractual | Harness `SseFrame` representation |
| HTML | raw `body`, parsed element attributes, text nodes | No whitespace, text, tag, or entity erasure | Rendered text, escaping, and DOM structure must remain reviewable | `test_html_normalization_retains_text_and_dom_changes` |
| static resources | bytes | SHA-256 exact hash; no content normalization | Asset bytes and content type are compatibility surfaces | `StaticObservation.from_bytes` |
| SQLite | schema and selected row counts | Stable ordering only; no page/WAL/timestamp comparison | SQL schema and durable effects matter, storage layout does not | `capture_database` and `test_python_config_and_database_observations_are_structured` |

## Explicitly forbidden

The harness must not drop unknown JSON keys, recursively remove fields by name,
canonicalize arbitrary HTML, ignore response text, normalize status codes or
exit codes, capture request bodies, or compare SQLite page-write counts.  A new
incidental difference requires a named field, rationale, and regression test
before it is added to this table.  A supported product difference requires an
ADR or a canonical cutover decision; it cannot be hidden here.

## Adding a differential case

1. Add a small fixture under `tests/migration_rs/fixtures/` with no credentials
   or real provider endpoint.
2. Launch Python with `PythonLauncher` and Rust with `RustLauncher`; call
   `assert_distinct_implementations` before collecting observations.
3. Compare structured observations with `compare_observations`, passing the
   isolated environment root as the only path root when appropriate.
4. If a failure is genuinely incidental, update this policy and its regression
   test in the same change.  If it is contractual, keep the failure visible
   and resolve it through the migration planning hierarchy.

The first Rust candidate does not implement config, HTTP, SQLite, or SSR yet.
Those cases therefore collect Python seed observations now and are ready to
become two-sided differential cases as F003–F005 land.
