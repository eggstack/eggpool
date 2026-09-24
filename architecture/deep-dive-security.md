# Deep Dive: Security and Redaction

Back to [Architecture](README.md)

The native runtime applies request limits, API-key authentication, header
filtering, credential redaction, safe filesystem permissions, and metadata-only
diagnostics. Configuration and deployment operations preserve secret values in
the environment or adjacent `.env`; they do not echo them into logs or
structured events.

Provider credentials are added only at dispatch-header construction. Raw
request/response bodies, prompts, cache keys, and token values are excluded
from persistence and operational snapshots. Control sockets and state files
use owner-only permissions and fail closed when ownership is ambiguous.

The downstream EggServe HTTP/1 parser applies explicit header, target, and
transport body ceilings before the Axum application. The fixed 1 GiB transport
body ceiling is defense in depth: EggPool's authenticated inference middleware
enforces the lower live generation limit while streaming the body and retains
the existing 413 response contract. EggServe's Tower bridge does not move auth,
dashboard exemptions, or integration-profile protection out of EggPool.
