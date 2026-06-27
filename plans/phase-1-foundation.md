# Phase 1 — Foundation

## Goal

Land the data model, configuration surface, and helper modules the rest of
the work depends on, **without changing runtime behaviour**. After this
phase, the codebase has the vocabulary for transcoding; nothing actually
transcodes yet. The single behavioural change is that
`ProxyRequestContext` carries an `upstream_protocol` field, which the rest of
the pipeline reads but which equals `protocol` until phase 4 turns the
selector on.

## Scope

In scope:

- New module `src/eggpool/transcoder/` with the helper utilities that all
  later phases consume.
- `TranscoderPolicy` Pydantic config model and `[transcoder]` config
  section.
- `upstream_protocol: str` and `transcode_required: bool` fields on
  `ProxyRequestContext`.
- Mechanical refactor: every read of `context.protocol` inside the
  coordinator that pertains to upstream-side concerns switches to
  `context.upstream_protocol`. Today they are equal; the rename is
  intentional and reviewable.
- Routing and eligibility accept an optional `transcode_eligibility`
  parameter. Default behaviour is identical to today.
- Unit tests for the helper modules (ids, usage, errors).

Out of scope (later phases):

- Body translation (phase 2).
- Streaming translation (phase 3).
- Widened routing (phase 4 — this phase only adds the parameter).
- Operator rollout and docs (phase 5).

## Files to create

```
src/eggpool/transcoder/
├── __init__.py             # public exports: TranscoderPolicy, helpers
├── policy.py               # TranscoderPolicy config model
├── context.py              # TranscodeContext dataclass (per-request state)
├── ids.py                  # tool-call-id translation map (phase-2 use)
├── usage.py                # usage-blob canonicalisation (phase-2 use)
└── errors.py               # upstream-error-envelope parser (phase-2 use)

tests/unit/test_transcoder/
├── __init__.py
├── test_policy.py
├── test_ids.py
├── test_usage_canonical.py
└── test_errors_parse.py
```

## Files to modify

```
src/eggpool/models/config.py          # add TranscoderPolicy to AppConfig
src/eggpool/request/coordinator.py    # upstream_protocol on context; reads refactored
src/eggpool/api/proxy_request.py      # initialize upstream_protocol = protocol
src/eggpool/routing/router.py         # accept transcode_eligibility selector
src/eggpool/routing/eligibility.py    # accept transcode_eligibility selector
src/eggpool/accounts/registry.py      # add account_supports_protocol_any
src/eggpool/catalog/cache.py          # add get_transcodable_protocols helper
src/eggpool/app.py                    # register TranscoderPolicy at startup
```

No behavioural change to existing tests.

## Detailed design

### 1. `TranscoderPolicy` (`src/eggpool/transcoder/policy.py`)

```python
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from eggpool.catalog.protocols import ProtocolName


class TranscoderPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description=(
            "When false (default), every request must match its upstream "
            "protocol exactly. When true, requests are transcoded when the "
            "selected account does not natively support the client protocol."
        ),
    )

    loss_policy: Literal["warn", "reject"] = Field(
        default="warn",
        description=(
            "How to handle loss-of-information during transcoding. 'warn' "
            "emits a structured log per request. 'reject' returns a 400. "
            "Only 'warn' is implemented in v1."
        ),
    )

    prefer_native: bool = Field(
        default=True,
        description=(
            "When true, native-protocol accounts outrank transcodable ones "
            "during routing regardless of routing_priority. When false, "
            "transcodable accounts may outrank native ones if their "
            "routing_priority is higher."
        ),
    )
```

Wired into `AppConfig` as a top-level optional field:

```python
# src/eggpool/models/config.py
transcoder: TranscoderPolicy = Field(default_factory=TranscoderPolicy)
```

### 2. `TranscodeContext` (`src/eggpool/transcoder/context.py`)

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import MutableMapping


@dataclass(slots=True)
class TranscodeContext:
    """Per-request transcoder state.

    Carries loss-of-information warnings and per-request id maps. One
    instance is constructed by handle_proxy_request and threaded through
    the coordinator for the lifetime of the request.
    """

    request_id: str
    client_protocol: str
    upstream_protocol: str

    # The set of protocol-mismatch warnings observed during this
    # request. Each entry is a structured dict suitable for log emission.
    # Never fatal in v1; populated by phase-2 translators.
    loss_warnings: list[dict[str, Any]] = field(default_factory=list)

    def is_native(self) -> bool:
        """True if no transcoding is required for this request."""
        return self.client_protocol == self.upstream_protocol
```

The `id_map` (for tool-call-id translation) is intentionally not on
`TranscodeContext` yet — it is created lazily by `ids.py` only when tools
land in phase 6. Keeping it out now avoids carrying unused state.

### 3. `ProxyRequestContext` extension (`src/eggpool/request/coordinator.py`)

Add two fields next to the existing `protocol`:

```python
@dataclass(slots=True)
class ProxyRequestContext:
    # ... existing fields ...
    upstream_protocol: str = ""        # set in handle_proxy_request
    transcode_required: bool = False  # set in handle_proxy_request
```

Default values keep the existing constructors source-compatible.

In `handle_proxy_request` (`api/proxy_request.py:174-185`), initialize them
as:

```python
upstream_protocol=endpoint.protocol,   # same as protocol today
transcode_required=False,             # no transcoding in phase 1
```

This is the **only** behaviour-affecting change in phase 1, and it is a
no-op until phase 4 widens the selector. The reason for landing it now is
to make every downstream reader of `context.protocol` switch to
`context.upstream_protocol` while the two are still equal. That way phase 4
is a one-line change at the entry point instead of a sweep.

### 4. Mechanical refactor of `context.protocol` reads

Search-replace is **not** the right tool here — the change is selective.
The rule:

- **Reads that pertain to the client side** (which endpoint to use to
  answer the client, what error envelope to render, what SSE format the
  client expects) **keep reading `context.protocol`**.
- **Reads that pertain to the upstream side** (which URL to dispatch to,
  which static headers to inject, which non-stream usage extractor to use,
  which SSE observer to construct) **switch to `context.upstream_protocol`**.

Concrete list of sites this phase touches:

| Site | Today | Phase 1 |
|---|---|---|
| `_validate_endpoint` (`coordinator.py:2074`) | uses `context.protocol` to compare against `model_protocols` | unchanged — this is a client-side check; the model protocol is what we are trying to surface to the client |
| `_get_upstream_url(protocol, provider_id)` (`coordinator.py:1611`) | called with `context.protocol` | caller passes `context.upstream_protocol` |
| `_build_upstream_headers(context, selected)` (`coordinator.py:1633`) | reads `context.protocol` indirectly | unchanged — `build_upstream_headers` itself reads `provider_cfg.auth` and `provider_cfg.headers`, not `protocol`. But we add an explicit `protocol=context.upstream_protocol` parameter to the provider contract's `build_upstream_headers` so the static-headers step knows which protocol's headers to compose. See `providers/contract.py` change below. |
| `_execute_non_streaming` (`coordinator.py:850`) | reads `context.protocol` for `_get_upstream_url` | switch to `context.upstream_protocol` |
| `_execute_streaming` (`coordinator.py:1047`) | reads `context.protocol` for `_get_upstream_url` and `stream_options` injection | switch to `context.upstream_protocol` |
| `_extract_non_stream_usage(protocol, body, ...)` (`coordinator.py:1456`) | called with `context.protocol` | caller passes `context.upstream_protocol` |
| `_build_stream_generator` (`coordinator.py:1219`) | constructs `IncrementalSSEObserver(context.protocol, ...)` | switch to `context.upstream_protocol` |

The mechanical sweep is small (7 sites). Each is a single-token rename in
the right direction. Every existing test continues to pass because
`upstream_protocol == protocol` everywhere today.

### 5. Provider contract header composition (`providers/contract.py`)

Today `build_upstream_headers` (`providers/contract.py:103`) does not know
which protocol the upstream speaks; it just emits `provider_cfg.headers` and
the auth header. With transcoding, an operator may want protocol-specific
static headers — for example, when transcoding an OpenAI request to an
Anthropic upstream, the `anthropic-version: 2023-06-01` header must be
injected even if the operator did not declare it under `providers.<id>.headers`.

Extend the signature:

```python
def build_upstream_headers(
    provider: ProviderConfig,
    api_key: str,
    *,
    protocol: ProtocolName | None = None,   # NEW: optional explicit protocol
) -> dict[str, str]: ...
```

When `protocol` is supplied **and** the provider does not already declare
protocol-required static headers, inject them from a small built-in table:

```python
# src/eggpool/transcoder/static_headers.py
PROTOCOL_REQUIRED_STATIC_HEADERS: dict[str, dict[str, str]] = {
    "anthropic": {"anthropic-version": "2023-06-01"},
}
```

If the operator declared `anthropic-version` under
`providers.<id>.headers`, that wins. If they declared a conflicting value,
today's `validate_static_headers` validator already rejects the conflict
(`models/config.py:498-515`), so the only remaining case is "operator did
not declare it" — the new code injects the default.

When `protocol is None` (today's behaviour, default), nothing is injected.
Operators who never enable transcoding see no change.

### 6. Routing and eligibility selector parameter

`get_eligible_account_names`, `select_accounts_for_failover`, and
`get_eligible_accounts` (`routing/eligibility.py:33`,
`routing/router.py:180`, `routing/router.py:198`) accept an optional
`transcode_eligibility: set[str] | None = None` parameter. When `None`,
behaviour is identical to today (strict protocol match). When supplied,
`account_supports_protocol_any` is used instead.

```python
# src/eggpool/accounts/registry.py
def account_supports_protocol_any(
    self,
    account_name: str,
    protocols: Iterable[str],
) -> bool:
    """Return whether an account supports any of the given protocols."""
    provider_id = self.get_provider_for_account(account_name)
    if provider_id is None:
        return False
    provider_protocols = self.get_provider_protocols(provider_id)
    return any(p in provider_protocols for p in protocols)
```

### 7. Catalogue helper

```python
# src/eggpool/catalog/cache.py
def get_transcodable_protocols(
    self,
    model_id: str,
    *,
    client_protocol: ProtocolName,
) -> set[ProtocolName]:
    """Return the set of protocols a model is reachable under, given a
    client protocol and the union of all account provider.protocols."""
    supporting = self.get_supporting_accounts(model_id)
    protocols: set[ProtocolName] = set()
    for account_name in supporting:
        provider_id = self._account_providers.get(account_name)
        if provider_id is None:
            continue
        for proto in self.get_provider_protocols(provider_id):
            protocols.add(proto)
    protocols.discard(client_protocol)
    return protocols
```

Used by phase 4's widened selector.

### 8. App startup wiring

`create_app` (`app.py`) instantiates `TranscoderPolicy` from `AppConfig`
and stores it on `app.state.transcoder_policy`. The coordinator reads
`app.state.transcoder_policy.enabled` when phase 4 starts gating
behaviour; in phase 1, the field is read but the gate is always `false`.

## Validation

After implementation:

```bash
uv run ruff format --check src/ tests/
uv run ruff check src/ tests/
uv run pyright src/
uv run pytest tests/unit/test_transcoder/ -v
uv run pytest tests/                        # full suite — must remain green
```

Acceptance criteria:

- `[transcoder] enabled = false` is the default; existing configs parse
  unchanged.
- `ProxyRequestContext.upstream_protocol == protocol` for every request
  in the existing test suite.
- New unit tests cover:
  - `TranscoderPolicy` field defaults, `extra="forbid"`, and round-trip
    through TOML.
  - `account_supports_protocol_any` against a three-account fixture with
    mixed `provider.protocols`.
  - `get_transcodable_protocols` against a fixture with two providers
    serving one model under different protocol sets.
  - `PROTOCOL_REQUIRED_STATIC_HEADERS` only injects when not already
    declared; existing operator declarations win.
- No change to `_validate_endpoint` semantics — `ProtocolMismatchError`
  still raises when the model is unknown, and the existing 503/400 paths
  remain untouched.

## Definition of done (phase 1)

- All files listed in "Files to create" exist and have at least one
  unit test each.
- All files listed in "Files to modify" are merged and the existing
  test suite remains green with zero changes to test files.
- `git grep -n 'context\.protocol' src/eggpool/request/coordinator.py`
  shows only client-side uses; upstream-side uses are now
  `context.upstream_protocol`.
- `[transcoder]` is documented in `config.example.toml` as a commented
  section.
- The roadmap is updated to mark phase 1 complete.