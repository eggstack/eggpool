# Phase 6 — Reject Policy Implementation

## Goal

When `loss_policy = "reject"` is set in `[transcoder]`, requests whose
translation would lose information are rejected with HTTP 400 before
dispatch instead of silently dropping the field.

## Implementation

The reject policy is implemented in `src/eggpool/api/proxy_request.py`
inside `preflight_request()`, after the body transcoder's
`encode_request()` returns its `loss_warnings` list.

```python
if self._transcoder_policy.loss_policy == "reject" and loss_warnings:
    return self._format_loss_policy_rejection(loss_warnings)
```

`_format_loss_policy_rejection()` builds an HTTP 400 response body
following the OpenAI error schema:

```json
{
  "error": {
    "message": "Request rejected by loss_policy=reject: ...",
    "type": "invalid_request_error",
    "code": "loss_policy_reject",
    "param": null
  }
}
```

The message lists the first few loss-warning kinds so the caller knows
what would be lost (e.g. `"dropped_field:top_p"`).

## Configuration

```toml
[transcoder]
loss_policy = "reject"   # default: "warn"
```

When `loss_policy = "warn"` (the default), loss warnings are logged and
appended to the response metadata but the request proceeds normally.

When `loss_policy = "reject"`, any non-empty `loss_warnings` list
causes an immediate 400 rejection before the upstream is contacted.

## Acceptance criteria

- `loss_policy = "reject"` returns HTTP 400 with a descriptive error
  message when translation produces loss warnings.
- `loss_policy = "warn"` preserves existing behaviour (warnings logged,
  request proceeds).
- The loss-warning catalogue test (`test_loss_warning_catalogue`) keeps
  all registered kinds in sync with actual emissions.
