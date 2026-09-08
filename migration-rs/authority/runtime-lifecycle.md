# Runtime-lifecycle authority audit

This table is the test-visible authority inventory for R010. `AppState` owns
only process/database handles and constructor-owned route/auth settings;
generation-owned values are obtained from `RuntimeManager` and held by a lease
for the whole operation.

| Production surface | Dependency | Authority | Lease boundary |
|---|---|---|---|
| Authentication middleware | API key, dashboard public topology | constructor-owned `ServerState` | none; restart-required by R005 |
| Inference body admission | `server.max_request_body_bytes` | active `RuntimeGeneration` | before body collection through handler completion |
| Chat/Messages/Responses | M7 inference graph, routing, wire state | active `RuntimeGeneration` | one `GenerationLease` from body admission through finite/stream execution |
| Readiness | configured accounts/credentials plus catalog, durable DB probe | active generation plus process DB | one generation lease across the probe |
| Dashboard overview/summary | route topology, theme, refresh interval, DB rollups | constructor-owned `ServerState` plus process DB | no live generation field is read |
| Static assets/theme route | route topology and bundled assets | constructor-owned `ServerState` / static assets | none |
| Recurring generation tasks | catalog/router/retention services | manager lookup per tick | one lease per tick; no loop captures a generation |
| Process tasks | DB/coalescer/process services | `ProcessRuntime` | process lifetime |
| Runtime diagnostics | active/retiring slots, task supervisor, reload store | manager/process stores | active metadata sampled in one snapshot call |
| Reload service | active generation config and candidate factory | `RuntimeManager` plus `ProcessRuntime` | candidate/stage/accept transaction boundary |
| Shutdown | phase, task/body tracking, manager and DB close | `ServerRuntime` / `ProcessRuntime` | explicit quiesce → drain → close ordering |

`AppState::from_inference` is a compatibility/test constructor. It creates a
manager-owned generation and is not used by the production startup paths,
which use `RuntimeGenerationFactory` before constructing `ServerRuntime`.

The audit intentionally does not classify M9 control/CLI transport, new
dashboard pages, or deferred process callbacks as M8 production surfaces.
