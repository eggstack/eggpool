# Phase 4 — Routing and eligibility

## Goal

Allow the routing layer to **select accounts whose provider supports the
model natively but not the client's protocol**, provided the
`TranscoderPolicy.enabled` flag is on. This is the phase that closes the
loop: a request can now actually reach an upstream that does not speak
the client's protocol. Phases 2 and 3 already wired the translation; this
phase widens the candidate set so translation actually has work to do.

After this phase, the operator-visible behaviour for the
`MiniMax International` scenario is:

```toml
[providers.minimax]
id = "minimax"
base_url = "https://api.minimax.io/anthropic"
protocols = ["anthropic"]
# (existing template)

[transcoder]
enabled = true
prefer_native = true
```

An OpenCode client posting to `/v1/chat/completions` with
`model: "MiniMax-M2.7/minimax"` reaches the `minimax` provider despite
its `protocols = ["anthropic"]` declaration. The model resolves to
`anthropic` in the catalogue, the transcoder translates OpenAI ↔ Anthropic
in both directions, and the response is rendered as OpenAI.

## Scope

In scope:

- Coordinator computes `upstream_protocol` for each candidate account
  before selection.
- `_validate_endpoint` (`coordinator.py:2053`) is bypassed when the
  model's native protocol is reachable through a transcodable account
  and `[transcoder] enabled = true`.
- Routing widens the candidate set to include accounts whose
  `provider.protocols` includes the model's **native** protocol even if
  it does not include the client protocol.
- `prefer_native = true` (default) keeps accounts whose `provider.protocols`
  includes the client protocol ranked above transcodable ones.
- `prefer_native = false` allows transcodable accounts to outrank native
  ones when their `routing_priority` is higher.
- Preflight `_check_context_limits` (`api/proxy_request.py:150`) runs a
  second pass on the translated upstream payload when transcoding is
  active. Both limits are honoured; the more restrictive wins.
- Integration test: end-to-end MiniMax scenario through the real app.

Out of scope:

- Operator rollout defaults. Phase 5.
- Docs / README updates. Phase 5.
- Catalog exposure changes. The model is still listed under its native
  protocol; clients continue to see `protocol: "anthropic"` in
  `/v1/models`. Clients that hard-filter by `protocol` still see what
  they saw before. The transcoder bridges at request time, not catalogue
  time.

## Files to modify

```
src/eggpool/api/proxy_request.py            # second-pass context-limit check
src/eggpool/request/coordinator.py          # upstream_protocol per-attempt; widened selector
src/eggpool/routing/router.py               # accept transcode_eligibility
src/eggpool/routing/eligibility.py          # widen protocol filter
src/eggpool/catalog/cache.py                # helper used by coordinator
```

## Files to create

```
tests/integration/
├── test_transcode_routing.py              # multi-account failover with transcoding
└── test_transcode_minimax_e2e.py          # the canonical MiniMax scenario

tests/unit/
└── test_routing_transcode_eligibility.py  # pure routing/eligibility tests
```

## Detailed design

### 1. Per-attempt `upstream_protocol` computation

Today, `_validate_endpoint` (`coordinator.py:2053`) raises
`ProtocolMismatchError` if the model's resolved protocols do not include
`context.protocol`. With transcoding on, the rule changes:

- If the model's resolved protocols include the client protocol, no
  transcoding is needed: `upstream_protocol = protocol`.
- Otherwise, if there exists at least one eligible account whose
  `provider.protocols` contains **any** of the model's resolved
  protocols, transcoding is enabled: pick that protocol as
  `upstream_protocol`. If multiple resolved protocols are available,
  prefer the one with the most eligible accounts; ties broken by the
  alphabetical order of the protocol name.
- Otherwise, no transcodable route exists: raise
  `ProtocolMismatchError` (or `ModelUnavailableError` if no model
  resolution), preserving today's error shape.

The selection happens inside `execute()` after `_validate_endpoint` and
before `_select_and_persist_attempt`. The chosen `upstream_protocol`
flows into the widened selector and into the body translator.

Pseudocode for the new pre-attempt step:

```python
def _resolve_upstream_protocol(
    self, context: ProxyRequestContext,
) -> str | None:
    model_protocols = self._catalog.cache.get_model_protocols(
        context.model_id, provider_id=context.provider_id,
    )
    if context.protocol in model_protocols:
        return context.protocol  # native match
    if not self._transcoder_policy.enabled:
        return None  # behaviour identical to today

    # Find transcodable protocols among all eligible accounts.
    candidates = (
        self._catalog.cache.get_transcodable_protocols(
            context.model_id, client_protocol=context.protocol,
        )
    )
    if not candidates:
        return None

    # Choose the protocol with the largest eligible-account set.
    counts = {
        p: self._catalog.cache.count_eligible_accounts_for_protocol(
            context.model_id, p,
        )
        for p in candidates
    }
    return max(sorted(counts), key=lambda p: counts[p])
```

`_validate_endpoint` is then renamed `_validate_endpoint_or_transcode`
and modified to:

1. Reject when the model is unknown (`ModelNotFoundError`).
2. Reject when `model_protocols` is empty (`ModelUnavailableError`).
3. Accept when client protocol is in `model_protocols`.
4. Accept when transcoding is enabled and `_resolve_upstream_protocol`
   returns non-None.
5. Otherwise raise `ProtocolMismatchError` with the original message.

The error message in case 5 stays identical so existing client behaviour
is preserved when transcoding is off.

### 2. Widened selector

`_select_and_persist_attempt` (`coordinator.py:485`) calls
`self._router.get_eligible_account_names(model_id, ..., protocol=context.protocol)`
(line 506-513). The new selector widens this:

```python
all_eligible = self._router.get_eligible_account_names(
    context.model_id,
    exclude_accounts=...,
    provider_id=context.provider_id,
    protocol=context.upstream_protocol,   # native-protocol match
    transcode_eligibility=(
        {context.protocol, context.upstream_protocol}
        if context.transcode_required else None
    ),
)
```

The router:

- Filters accounts whose `provider.protocols` includes the
  `protocol` parameter (today's behaviour).
- Additionally includes accounts whose `provider.protocols` includes any
  protocol in `transcode_eligibility`. Such accounts are tagged
  `requires_transcode: True` on the candidate.
- The scorer (`QuotaFairScorer`) ranks candidates, with
  `requires_transcode` accounts placed **after** native ones when
  `prefer_native = true`.

### 3. Scorer integration

Today `QuotaFairScorer` orders by `routing_priority` descending then by
score within tier. Add a **secondary key** when transcoding is enabled
and `prefer_native = true`: `requires_transcode = False` ranks above
`requires_transcode = True` regardless of priority.

When `prefer_native = false`, the secondary key is omitted.

Implementation:

```python
# src/eggpool/routing/scorer.py (existing module)
sorted_candidates = sorted(
    candidates,
    key=lambda c: (
        -c.routing_priority,
        0 if not c.requires_transcode else 1,   # prefer_native
        c.final_score,
    ),
)
```

The scorer already orders by negative priority; this is the same key with
an extra dimension inserted before the score.

### 4. Account-state extension

`AccountRuntimeState` gains a transient attribute
`requires_transcode: bool` populated by the router when the candidate
was included via the widened selector. This propagates to
`SelectedAttempt` (`coordinator.py:142`) so the rest of the pipeline
knows whether to invoke the body translator.

```python
@dataclass(frozen=True, slots=True)
class SelectedAttempt:
    # ... existing fields ...
    requires_transcode: bool = False
```

The coordinator reads this when deciding whether to call
`select_transcoder(...)` (phase 2).

### 5. `_check_context_limits` two-pass

`api/proxy_request.py:150` currently runs once with the client payload.
Phase 4 makes it run twice when transcoding is required:

```python
catalog = getattr(request.app.state, "catalog", None)
policy = getattr(request.app.state, "transcoder_policy", None)
if catalog is not None:
    _check_context_limits(
        model_id=model_id, provider_id=provider_id,
        body=body, payload=payload,
        protocol=endpoint.protocol,
        catalog_cache=catalog.cache,
    )
    if policy is not None and policy.enabled:
        # Translate the client payload once for the second pass.
        # The transcoder selection here is purely speculative — it
        # runs even if no transcodable upstream exists, because the
        # check is cheap and the alternative is two different limits
        # for the same client request.
        upstream_protocol = _infer_upstream_protocol(
            catalog, model_id, endpoint.protocol,
        )
        if upstream_protocol and upstream_protocol != endpoint.protocol:
            transcoder = select_transcoder(
                client_protocol=endpoint.protocol,
                upstream_protocol=upstream_protocol,
            )
            if transcoder is not None:
                translated, _ = transcoder.encode_request(
                    payload, TranscodeContext(...)
                )
                _check_context_limits(
                    model_id=model_id, provider_id=provider_id,
                    body=encode_json_body(translated),
                    payload=translated,
                    protocol=upstream_protocol,
                    catalog_cache=catalog.cache,
                )
```

The second pass only adds an upper bound check; the client-visible limit
is still the client-side one. Both must pass.

`_infer_upstream_protocol` is a thin wrapper around
`_resolve_upstream_protocol` that doesn't raise on miss.

### 6. Cache helper

`get_transcodable_protocols` was defined in phase 1
(`catalog/cache.py`). Phase 4 adds one helper to count eligible accounts
per protocol, used by `_resolve_upstream_protocol`:

```python
def count_eligible_accounts_for_protocol(
    self,
    model_id: str,
    protocol: str,
) -> int:
    """Count enabled accounts whose provider supports `protocol` and
    has the model in its catalogue."""
    supporting = self.get_supporting_accounts(model_id)
    n = 0
    for account_name in supporting:
        provider_id = self._account_providers.get(account_name)
        if provider_id is None:
            continue
        if protocol in self.get_provider_protocols(provider_id):
            n += 1
    return n
```

### 7. Migration safety

No database migration is required: every new field is on
`ProxyRequestContext` (per-request, in-memory) or `SelectedAttempt`
(per-attempt, in-memory) or `AccountRuntimeState` (transient, populated
at runtime). The on-disk schema is unchanged.

## Validation

After implementation:

```bash
uv run ruff format --check src/ tests/
uv run ruff check src/ tests/
uv run pyright src/
uv run pytest tests/unit/test_routing_transcode_eligibility.py -v
uv run pytest tests/integration/test_transcode_routing.py -v
uv run pytest tests/integration/test_transcode_minimax_e2e.py -v
uv run pytest tests/                                      # full suite
```

Acceptance criteria:

- Existing routing tests (`tests/integration/test_proxy_integration.py`,
  `tests/integration/test_failover_matrix.py`,
  `tests/integration/test_upstream_authoritative_suppression.py`) remain
  green **without modification**.
- New integration test reproduces the user's scenario: OpenAI client →
  `MiniMax-M2.7/minimax` → mocked `https://api.minimax.io/anthropic`
  Anthropic upstream → 200 OK with OpenAI-shaped response.
- New failover test: when transcoding is off, an OpenAI request to a
  model whose only provider is Anthropic-only fails with
  `ProtocolMismatchError` (today's behaviour); when on, it succeeds.
- Routing priority test: with `prefer_native = true`, a transcodable
  account with `routing_priority = 10` is outranked by a native
  account with `routing_priority = 0`. With `prefer_native = false`,
  the transcodable account wins.
- Two-pass context-limit test: an OpenAI request with `max_tokens: 8000`
  against a transcoded upstream that accepts only `max_tokens: 4096` is
  rejected; the same request against a transcoded upstream that
  accepts `max_tokens: 16384` is allowed.

## Definition of done (phase 4)

- All files in "Files to modify" and "Files to create" are merged with
  passing tests.
- The canonical MiniMax scenario works end-to-end against a mocked
  upstream.
- No existing test changes behaviour.
- `upstream_protocol` is resolved for every request, even same-protocol
  ones (where it equals `protocol`).
- Roadmap updated; phase 4 marked complete.