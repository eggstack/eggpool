# Plan 146 — Stopped: Upstream Documentation Does Not Match Plan Premise

Status: stopped without implementation

Date: 2026-08-19

Parent plan:

- `plans/146-ollama-image-url-metadata-corrective-closure.md`

Corrective baseline:

- `9ba1074a1bbdf84232305569d849617a78014673`

## Outcome

Plan 146 was not implemented. Its premise — that the current official
Ollama OpenAI-compatibility page documents `Image URL` as a supported
form for `/v1/chat/completions` image content — is not supported by
the live page, by the recent Plan 145 verification commit, or by the
existing regression test's docstring.

Per Plan 146's own stop rule:

> If the official page has materially changed and no longer documents
> Image URL support, stop this plan and record the exact changed
> upstream evidence rather than implementing from stale plan text.

implementation has been skipped and the evidence is recorded here. No
production file, test file, or documentation was changed.

## Upstream evidence — live page, fetched 2026-08-19

Source: <https://docs.ollama.com/api/openai-compatibility>

Raw HTML extracted for `/v1/chat/completions` → `Supported request fields`
→ `messages` → `Image content`:

```html
<li class="task-list-item"><input type="checkbox" disabled="" checked=""/> <!-- -->Base64 encoded image</li>
<li class="task-list-item"><input type="checkbox" disabled=""/> <!-- -->Image URL</li>
```

Checkbox convention is consistent across the page (e.g. `[x] Chat
completions` vs `[ ] Logprobs`, `[x] Stateful requests` absent for
`/v1/responses`):

- `checked=""` ⇒ feature is supported
- attribute omitted ⇒ feature is not supported

Therefore the page currently states:

- `Base64 encoded image` — **supported**
- `Image URL` — **not supported**

The page's vision examples also all use `data:image/png;base64,…`
URIs delivered via `image_url`, which is base64 carried in the
`image_url` content type — not a remote URL. There is no example on
the page that uses a remote `https://…` image.

## Prior verification commit — Plan 145

`9ba1074a` "Implement Plan 145: Plan 144 final corrective patch"
contains this Workstream B statement:

> Workstream B (Ollama metadata) verifies current Ollama OpenAI-
> compatibility documentation still reports Image URL as not
> supported: the bundled template, comment, and test all remain
> correct as `image_input.url = false`. No template/test change is
> required.

That commit is the most recent implementer-grade verification of this
exact fact on this codebase and is the source Plan 146 contradicted
without re-checking.

## Existing regression test is consistent with the docs

`tests/unit/test_plan_141_corrective_closure.py` lines 644-689
(`TestLocalProviderImageUrlMetadata`) currently asserts:

- Ollama `image_input.base64 is True`
- Ollama `image_input.url is False`
- llama.cpp `image_input.url is True`
- vLLM `image_input.url is True`

The Ollama assertion matches the live documentation. Plan 146's
intended flip to `url is True` would have made the bundled metadata
disagree with the official page.

The class docstring (lines 644-656) already accurately states the
docs say `Image URL` is not supported. No test edit is warranted.

## Source-of-truth discipline preserved

Plan 146 listed its official source of truth:

> `https://docs.ollama.com/api/openai-compatibility`

The page no longer documents `Image URL` support, and Plan 146's own
author explicitly told future implementers to stop if the page ever
stopped documenting it. The correct action is to stop and record the
upstream evidence, which is what this file does.

## Plans 131-146 closure

Plan 146's intended goal — closing the Plans 131-146 line with a
narrow metadata/test correction — was the only remaining item in
that line. With Plan 146 stopped on the basis above, the line is
otherwise closed at the Plan 145 implementation commit. No further
automatic closure plan should be created unless a new reproduced
defect or explicit requirement appears, per Plan 146's G-closure
guidance.
