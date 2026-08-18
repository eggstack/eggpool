# Plan 146 — Ollama Image URL Metadata Corrective Closure

Status: ready for implementation

Baseline reviewed: `9ba1074a1bbdf84232305569d849617a78014673`

Related plans: 131–145, especially Plans 144–145

## Purpose

Plan 145 correctly fixed the remaining OpenAI Responses stream-lifecycle defect and added the missing coordinator/ASGI seam coverage. One narrow correctness issue remains: EggPool's bundled Ollama capability metadata and its regression test still claim that the Ollama OpenAI-compatible Chat Completions endpoint does not accept Image URL content.

That claim conflicts with the current official Ollama OpenAI-compatibility documentation, which explicitly lists both `Base64 encoded image` and `Image URL` under supported image content for `/v1/chat/completions`.

Official source of truth to verify immediately before implementation:

- `https://docs.ollama.com/api/openai-compatibility`

This plan changes only that source-form capability fact and the test that protects it. It is not a new multimodal architecture phase, provider-adapter project, or Responses milestone.

When this plan's acceptance criteria pass, treat Plans 131–146 as closed. Do not create another automatic closure/polish plan unless a new reproduced defect or explicit requirement appears.

---

# Confirmed baseline defect

At baseline `9ba1074a1bbdf84232305569d849617a78014673`, the Ollama template contains:

```toml
[providers.ollama-local.model_capabilities.default.multimodal]
# Ollama supports base64 images through the OpenAI-compatible endpoint.
# URL images are not supported (Ollama fetches from data URIs only).
...
image_input = { base64 = true, url = false }
```

The corresponding regression test in `tests/unit/test_plan_141_corrective_closure.py` also asserts:

```python
assert image_input.get("base64") is True
assert image_input.get("url") is False
```

and its docstring attributes that `url = false` claim to current Ollama documentation.

The official Ollama OpenAI-compatibility documentation currently states that `/v1/chat/completions` supports image content supplied as:

- Base64 encoded image;
- Image URL.

Therefore EggPool is currently failing closed on a source form that the upstream-compatible endpoint documents as supported.

---

# Required end state

After Plan 146 lands:

```toml
[providers.ollama-local.model_capabilities.default.multimodal]
image_input = { base64 = true, url = true }
```

The adjacent comment must accurately describe the endpoint-level capability and must not claim that every Ollama model is vision-capable.

The existing local-provider metadata regression test must assert:

```python
assert image_input.get("base64") is True
assert image_input.get("url") is True
```

No other provider metadata, routing behavior, transcoding behavior, request mutation, provider selection, Responses behavior, dependency, CI job, or test tier should change.

---

# Scope

## Required production file

- `src/eggpool/providers/_templates.toml`

## Required test file

- `tests/unit/test_plan_141_corrective_closure.py`

## Optional documentation files

None are expected.

Only touch another file if it contains the same stale Ollama-specific statement that Image URL content is unsupported. If such a statement is found, correct that statement only; do not broaden documentation changes.

---

# Explicitly out of scope

Do not use this patch to:

- redesign EggPool's multimodal capability system;
- add a provider-specific Ollama request adapter;
- add an Ollama SDK or any provider SDK;
- perform live Ollama probing for image capabilities;
- infer that every model installed in Ollama supports vision;
- change model-level multimodal discovery semantics;
- add PDF, audio, video, or non-text tool-result support;
- add speculative request-size limits;
- change Ollama's `/v1/responses` support or `responses_path`;
- change Responses same-protocol routing;
- change OpenAI/Anthropic transcoding;
- alter request-size enforcement;
- alter retry, health, suppression, or backoff behavior;
- alter stream finalization behavior fixed by Plan 145;
- modify llama.cpp, vLLM, LM Studio, LocalAI, or generic provider metadata unless a directly adjacent shared comment must be kept syntactically coherent;
- add dependencies;
- add CI jobs, matrices, or new verification infrastructure;
- add integration tests, live-provider tests, Docker fixtures, or network-dependent tests;
- create a new provider capability abstraction.

This should be a tiny metadata/test correction.

---

# Invariants to preserve

## I1 — Source-form capability is not model capability

`image_input.url = true` means the Ollama OpenAI-compatible endpoint can represent/accept an image supplied using OpenAI's Image URL content form.

It does not mean every model served by Ollama can process images.

Do not rewrite the metadata or comments in a way that conflates:

```text
endpoint supports image_url source form
```

with:

```text
all Ollama models are multimodal
```

The loaded model remains the authority on whether vision is actually usable.

## I2 — Base64 support remains enabled

Preserve:

```toml
base64 = true
```

The correction is additive to the source-form declaration:

```text
before: base64=true, url=false
after:  base64=true, url=true
```

## I3 — No speculative additional capabilities

Preserve the conservative defaults for capabilities not established by this correction, including the existing document/audio/non-text-tool-result posture.

Do not infer additional modality support from the Image URL documentation.

## I4 — No request-size ceiling

Do not add `max_serialized_request_bytes` or another Ollama request-size limit as part of this patch.

## I5 — llama.cpp and vLLM remain unchanged

Their current URL-image source-form metadata is already `true` and is outside this correction.

## I6 — Plan 145 runtime behavior remains untouched

The Responses terminal-stream fix is complete. No runtime coordinator/finalizer code should be modified by Plan 146.

---

# Implementation steps

## Step 1 — Re-verify the authoritative source

Immediately before editing, open the current official Ollama page:

`https://docs.ollama.com/api/openai-compatibility`

Under `/v1/chat/completions` -> supported request fields -> `messages` -> image `content`, confirm the page still lists both:

- `Base64 encoded image`;
- `Image URL`.

If the official page has materially changed and no longer documents Image URL support, stop this plan and record the exact changed upstream evidence rather than implementing from stale plan text.

Do not substitute:

- blog posts;
- GitHub issue comments;
- forum posts;
- cached model output;
- the existing EggPool regression test;
- the Plan 145 implementation commit message.

The official Ollama documentation is the source of truth for this source-form metadata.

## Step 2 — Correct the Ollama template

Edit only the Ollama multimodal block in:

`src/eggpool/providers/_templates.toml`

Change:

```toml
image_input = { base64 = true, url = false }
```

to:

```toml
image_input = { base64 = true, url = true }
```

Replace the stale comment:

```text
URL images are not supported ...
```

with wording equivalent to:

```text
Ollama's OpenAI-compatible Chat Completions endpoint accepts both
base64 image content and Image URL content. Actual vision support
still depends on the loaded model.
```

Keep the comment concise. Do not add implementation history or plan-number prose to the provider template.

## Step 3 — Correct the existing regression test

In:

`tests/unit/test_plan_141_corrective_closure.py`

update `TestLocalProviderImageUrlMetadata` so the Ollama case asserts:

```python
assert image_input.get("base64") is True
assert image_input.get("url") is True
```

Rename the test if needed from a `..._is_false` name to a `..._is_true` name.

Correct the class/test docstring so it accurately states that current official Ollama OpenAI compatibility supports both Base64 encoded images and Image URL content.

Preserve the existing llama.cpp and vLLM assertions unchanged.

Do not create a second Ollama metadata test elsewhere. The existing compact template-regression test is the correct seam.

## Step 4 — Search for the stale claim

Perform one repository search for Ollama-specific statements equivalent to:

- `URL images are not supported`;
- `image_input.url = false`;
- `Ollama ... Image URL ... not supported`.

If the only stale instances are the provider template and the existing test, make no additional edits.

If another user/developer-facing file repeats the same false capability claim, correct only that sentence.

Do not perform a broad documentation rewrite.

## Step 5 — Run focused verification

Run the smallest useful checks first:

```bash
pytest -q tests/unit/test_plan_141_corrective_closure.py
```

Then run the repository's existing normal local verification commands appropriate for a metadata/test-only patch, without adding new CI or test infrastructure.

At minimum, preserve the repository's current lint/type/test expectations if they are already part of the standard local workflow.

Do not add a live Ollama network test: this capability is deliberately protected by static provider metadata plus upstream documentation review.

---

# Acceptance criteria

All criteria below are required for Plan 146 closure.

## A — Authoritative evidence

- [ ] The implementer re-checks the current official Ollama OpenAI-compatibility documentation immediately before editing.
- [ ] The checked page is the official `docs.ollama.com` OpenAI-compatibility page, not a secondary source.
- [ ] The current page still lists `Base64 encoded image` for `/v1/chat/completions` image content.
- [ ] The current page still lists `Image URL` for `/v1/chat/completions` image content.
- [ ] If upstream documentation no longer contains Image URL support, implementation stops rather than forcing this plan's expected value.

## B — Provider metadata

- [ ] `providers.ollama-local.model_capabilities.default.multimodal.image_input.base64` remains `true`.
- [ ] `providers.ollama-local.model_capabilities.default.multimodal.image_input.url` is `true`.
- [ ] The adjacent Ollama comment no longer says URL images are unsupported.
- [ ] The adjacent comment makes clear, directly or by preserving existing model-capability semantics, that actual vision support depends on the loaded model.
- [ ] No PDF/audio/video/tool-result capability is newly enabled.
- [ ] No serialized request-size ceiling is introduced.

## C — Regression coverage

- [ ] The existing `TestLocalProviderImageUrlMetadata` Ollama test expects `base64 is True`.
- [ ] The existing `TestLocalProviderImageUrlMetadata` Ollama test expects `url is True`.
- [ ] The Ollama test/docstring no longer attributes `url = false` to official documentation.
- [ ] llama.cpp URL-image assertions remain `true` and otherwise unchanged.
- [ ] vLLM URL-image assertions remain `true` and otherwise unchanged.
- [ ] No duplicate provider-metadata test framework or new test file is added solely for this correction.

## D — Runtime/non-regression scope

- [ ] `src/eggpool/request/coordinator.py` is unchanged by this patch.
- [ ] Responses stream completion/failure/incomplete behavior from Plan 145 is unchanged.
- [ ] Responses same-protocol routing is unchanged.
- [ ] OpenAI/Anthropic transcoding behavior is unchanged.
- [ ] provider selection, retry, suppression, backoff, and health behavior are unchanged.
- [ ] request-size enforcement is unchanged.
- [ ] model discovery behavior is unchanged.

## E — Dependency and CI restraint

- [ ] No dependency is added or removed.
- [ ] `pyproject.toml`/lock dependency changes are absent unless generated tooling rewrites metadata without semantic dependency changes; such incidental churn should be reverted.
- [ ] No provider SDK is added.
- [ ] No GitHub Actions job or matrix is added.
- [ ] Existing CI topology remains unchanged.
- [ ] No live-provider/network-dependent test is added.
- [ ] No Docker/service fixture is added.

## F — Verification

- [ ] `pytest -q tests/unit/test_plan_141_corrective_closure.py` passes.
- [ ] Existing repository lint/format checks applicable to the changed Python test pass.
- [ ] Existing type checks applicable to the changed test pass.
- [ ] Existing normal smoke verification remains green when run locally.
- [ ] The final diff contains only the minimal metadata/test correction plus, if necessary, a directly duplicated stale Ollama statement elsewhere.

## G — Closure

- [ ] The implementation commit can be explained as one factual metadata correction, not a new feature subsystem.
- [ ] No known Plan 145 acceptance defect remains after this correction.
- [ ] Plans 131–146 can be marked closed after verification.
- [ ] No further generic closure/polish plan is created unless a new reproduced defect or explicit requirement appears.

---

# Expected diff shape

The ideal implementation is approximately:

```text
src/eggpool/providers/_templates.toml
    1 boolean change
    1–3 comment-line edits

tests/unit/test_plan_141_corrective_closure.py
    1 assertion change
    optional test-name change
    small docstring correction
```

A substantially larger production diff is a warning that scope has expanded incorrectly.

---

# Handoff notes

This correction exists because Plan 145's implementation commit stated that current Ollama documentation reported Image URL as unsupported, while a subsequent independent check of the official OpenAI-compatibility page showed the opposite.

Do not re-litigate the broader multimodal architecture. Re-check the official page, make the metadata match it, update the existing test, run focused verification, and stop.

The runtime work from Plan 145 should not be touched.

---

# Completion rule

Plan 146 is complete when:

1. current official Ollama documentation has been re-verified;
2. EggPool's Ollama template matches that documented source-form support;
3. the existing metadata regression test matches the template and upstream fact;
4. focused/local repository verification passes; and
5. the patch introduces no unrelated runtime, dependency, CI, or test-harness changes.

At that point, close the Plans 131–146 line.