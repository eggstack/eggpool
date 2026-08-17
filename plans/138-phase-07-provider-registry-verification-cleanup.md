# Plan 138 — Phase 7: Provider Registry and Verification Cleanup

## Objective

Make `eggpool connect` provider coverage accurate and maintainable by reducing brittle fixed-model probes, curating experimental templates, and relying on generic compatible contracts where provider-specific templates add little value.

## Background

The current template registry includes many hosted providers with `verified`, `experimental`, and `unverified` status. Some verification blocks use a specific probe model. This is appropriate only when the provider guarantees that model. For dynamic/local catalogs it is brittle and can turn a healthy endpoint into an onboarding failure.

## Work items

### 1. Classify templates

For every bundled provider template, classify whether it needs provider-specific treatment because of:

- nonstandard base/path composition;
- auth/header differences;
- protocol-specific static headers;
- model-list behavior;
- known capability quirks.

If none apply, consider whether the generic compatible endpoint plus a curated shortcut is sufficient.

### 2. Verification policy

Adopt this hierarchy:

1. validate config/URL/auth shape locally;
2. model-list probe when supported;
3. choose an actually discovered model for an optional inference probe;
4. fixed model probe only when the provider contract guarantees it or the template explicitly documents why.

A provider with a valid endpoint but zero accessible models should produce a distinct diagnostic from authentication/network failure.

### 3. Status accuracy

Review `verified` vs `experimental` metadata. Do not mark a template verified based only on generic OpenAI compatibility assumptions. If current behavior cannot be verified from docs/tests/live evidence, downgrade rather than guess.

### 4. Connect UX

Group choices coherently: Local, Hosted Direct, Aggregator, Custom. Keep recommended flags sparse. Provider count is not itself a product goal.

### 5. Pruning

Remove or consolidate templates whose only distinction is a generic base URL and whose information is stale/unverifiable, provided this does not break documented existing installations. Existing user configs must continue to load even if a shortcut disappears from the onboarding menu.

### 6. Documentation

Document the difference between a bundled onboarding shortcut and protocol compatibility. A provider not listed in `connect` should still be supportable through the generic path when it speaks the compatible contract.

## Tests

Update existing provider contract/connect tests. Required cases:

- discovery-selected probe model;
- zero-model endpoint diagnostic;
- fixed probe retained only for explicit guaranteed-model template;
- no-auth local provider;
- provider-specific auth/header contract;
- generic custom compatible endpoint;
- existing config for a removed onboarding shortcut remains valid when manually configured.

## Acceptance criteria

- Fixed model probes are absent from providers where model presence is not guaranteed.
- Provider statuses/notes accurately describe verification confidence.
- Local and custom endpoints are easy to find in `connect`.
- Bundled template count does not grow unnecessarily just to list every compatible vendor.
- Generic OpenAI/Anthropic-compatible onboarding covers providers without special contracts.
- Existing user configs retain compatibility.
- No provider SDK dependency or new CI matrix.
