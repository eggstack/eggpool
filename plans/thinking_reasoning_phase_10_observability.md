# Phase 10: Observability for Thinking/Reasoning

## Objective

Add metrics, request trace fields, and dashboard/API surfaces that make thinking/reasoning behavior debuggable. Operators should be able to see when thinking was requested, translated, dropped, rejected, clamped, or routed based on capability.

## Problem statement

Thinking support crosses config, catalog metadata, router decisions, transcoder field mapping, provider response handling, and streaming. Without observability, users will see only symptoms: missing reasoning output, provider rejection, or unexpected routing. EggPool should expose structured diagnostics for this line of behavior.

## Metrics

Add counters with low-cardinality labels where appropriate:

- `thinking_requested_total`
- `thinking_transcoded_total`
- `thinking_dropped_total`
- `thinking_rejected_total`
- `thinking_unknown_capability_total`
- `thinking_unsupported_capability_total`
- `thinking_budget_clamped_total`
- `thinking_stream_deltas_total`
- `thinking_response_blocks_total`

Suggested labels:

- `client_protocol`
- `upstream_protocol`
- `provider_id`
- `capability_status`
- `decision`

Avoid high-cardinality labels such as full request ids or arbitrary model aliases in metrics unless the existing metrics system already uses them safely.

## Request trace fields

Add structured trace/debug fields to whatever request accounting or runtime trace mechanism already exists:

```json
{
  "thinking": {
    "requested": true,
    "client_protocol": "openai",
    "request_fields": ["reasoning_effort"],
    "requested_effort": "medium",
    "resolved_budget_tokens": 4096,
    "budget_clamped": false,
    "capability_status": "supported",
    "capability_source": "manual_override",
    "upstream_protocol": "anthropic",
    "upstream_fields": ["thinking"],
    "decision": "transcoded"
  }
}
```

## Dashboard/API surfaces

Add or extend surfaces to show:

- Per-model thinking support.
- Capability source.
- Whether a collapsed model has mixed support.
- Recent thinking request decisions.
- Counts of dropped/rejected thinking requests.

If the dashboard already has a model detail page, add capability metadata there first. If not, expose the data through the stats/runtime API and defer UI polish.

## Implementation tasks

1. Inspect existing stats/metrics/dashboard patterns.
2. Add low-cardinality counters for thinking decision outcomes.
3. Add request trace structure for thinking-related decisions.
4. Increment counters in request classification, routing rejection, transcoder translation, response decoding, and streaming delta translation paths.
5. Expose aggregate stats through the existing stats API.
6. Add dashboard display only after backend metrics are stable.
7. Add tests for metric increments on key paths.

## Acceptance criteria

- Operators can see how many requests asked for thinking.
- Operators can distinguish translated, dropped, rejected, and clamped cases.
- Request traces show the requested field, resolved upstream field, capability status, and capability source.
- Model UI/API surfaces show thinking support and mixed collapsed states.
- Metrics do not create unbounded cardinality.
- Tests cover at least requested, translated, dropped, rejected, and clamped counters.

## Risks

Metrics can become noisy if every provider/model alias is used as a label. Keep labels aligned with existing project practices.

Do not expose sensitive prompt or reasoning content in metrics. Only expose metadata about decisions and field names.

## Completion check

Send three test requests: one translated, one rejected due to unsupported capability, and one clamped budget. Confirm counters and request traces show distinct outcomes without storing prompt content or reasoning text.
