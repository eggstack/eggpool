//! Portable OpenCode rendering, variant selection, and JSONC-preserving
//! mutation.
//!
//! Two explicit schema variants are supported (Plan 213 workstreams 5-9):
//!
//! - V1 (`provider` / `npm` / `options`): qualified against OpenCode 1.18.30.
//!   Uses the Responses-capable `@ai-sdk/openai` runtime with
//!   `{env:EGGPOOL_API_KEY}` interpolation.
//! - V2 (`providers` / `package` / `settings`): current OpenCode V2 docs
//!   (fetched 2026-09-16 from `https://opencode.ai/v2/docs/providers/` and
//!   `/models/`). Uses the Responses-capable
//!   `@opencode/ai/providers/openai-compatible/responses` package with an
//!   `env: ["EGGPOOL_API_KEY"]` credential list and `settings.baseURL`.
//!
//! Both variants render from the same conservative provider-neutral
//! projection. Model entries expose only proven capabilities: public ID,
//! display name, context/output limits when known, text input/output, image
//! input only when guaranteed, and tool support only when exactly known
//! (unknown stays omitted rather than guessed). WebSocket transport is never
//! advertised. V2 reasoning-effort variants are deliberately omitted: the V1
//! `variants` map has no proven V2 `settings.reasoningEffort` equivalent for
//! the compatible/responses package, and default reasoning works without
//! them.
//!
//! Mutation preserves every unowned byte: comments, trailing commas,
//! indentation, ordering, unrelated providers, and nested settings are left
//! untouched; only the owned `eggpool` provider entry (and, when EggPool
//! creates it, its narrowly scoped parent key) is inserted, replaced, or
//! removed. Unknown/ambiguous documents fail closed to plan/manual output.

use std::collections::BTreeMap;

use serde_json::{Map, Value};

use crate::adapter::ClientSchemaVariant;
use crate::codex::EGGPOOL_API_KEY_ENV;
use crate::error::ClientConfigError;
use crate::jsonc::{self, Doc};
use crate::projection::AgentModelProjection;

/// NPM runtime for the Responses-capable OpenCode V1 provider.
pub const OPENCODE_RESPONSES_NPM: &str = "@ai-sdk/openai";
/// Runtime package for the Responses-capable OpenCode V2 provider.
///
/// Provenance: `https://opencode.ai/v2/docs/providers/` (2026-09-16) lists
/// `@opencode/ai/providers/openai-compatible/responses` as the Responses
/// wire over an OpenAI-compatible endpoint, which is exactly EggPool's
/// contract (custom `baseURL`, Responses `/v1/responses` surface). An older
/// cached index showed an `@opencode-ai/ai/...` prefix; that prefix is NOT
/// used because the live V2 docs are authoritative.
pub const OPENCODE_V2_RESPONSES_PACKAGE: &str =
    "@opencode/ai/providers/openai-compatible/responses";

/// Parent key for the V1 schema variant.
pub const OPENCODE_V1_PARENT: &str = "provider";
/// Parent key for the V2 schema variant.
pub const OPENCODE_V2_PARENT: &str = "providers";
/// Owned provider entry name in both variants.
pub const OPENCODE_OWNED_ENTRY: &str = "eggpool";

/// Parent key for a schema variant.
pub fn parent_key_for(variant: ClientSchemaVariant) -> &'static str {
    match variant {
        ClientSchemaVariant::OpencodeV2 => OPENCODE_V2_PARENT,
        _ => OPENCODE_V1_PARENT,
    }
}

/// Owned-field path for a schema variant (`provider.eggpool` / `providers.eggpool`).
pub fn owned_path_for(variant: ClientSchemaVariant) -> &'static str {
    match variant {
        ClientSchemaVariant::OpencodeV2 => "providers.eggpool",
        _ => "provider.eggpool",
    }
}

// ---------------------------------------------------------------------------
// Model entries (shared conservative projection)
// ---------------------------------------------------------------------------

fn limit_object(projection: &AgentModelProjection) -> Option<Map<String, Value>> {
    let mut limit = Map::new();
    if let Some(value) = projection
        .capabilities
        .context_tokens
        .filter(|value| *value > 0)
    {
        limit.insert("context".to_owned(), serde_json::json!(value));
    }
    if let Some(value) = projection
        .capabilities
        .max_output_tokens
        .filter(|value| *value > 0)
    {
        limit.insert("output".to_owned(), serde_json::json!(value));
    }
    if limit.is_empty() {
        None
    } else {
        Some(limit)
    }
}

fn display_name_entry(projection: &AgentModelProjection) -> Option<(String, Value)> {
    if projection.display_name != projection.public_id {
        Some((
            "name".to_owned(),
            Value::String(projection.display_name.clone()),
        ))
    } else {
        None
    }
}

/// V1 model entry: `name` / `limit` / `modalities` / `reasoning` / `variants`.
fn v1_model_entry(projection: &AgentModelProjection) -> Value {
    let mut entry = Map::new();
    if let Some((key, value)) = display_name_entry(projection) {
        entry.insert(key, value);
    }
    if let Some(limit) = limit_object(projection) {
        entry.insert("limit".to_owned(), Value::Object(limit));
    }
    let mut input_modalities = vec![Value::String("text".to_owned())];
    if projection.capabilities.input_images == Some(true) {
        input_modalities.push(Value::String("image".to_owned()));
    }
    entry.insert(
        "modalities".to_owned(),
        serde_json::json!({
            "input": input_modalities,
            "output": ["text"],
        }),
    );
    if let Some(reasoning) = &projection.capabilities.reasoning {
        entry.insert("reasoning".to_owned(), Value::Bool(true));
        if !reasoning.efforts.is_empty() {
            let mut variants = Map::new();
            for effort in &reasoning.efforts {
                variants.insert(
                    effort.to_owned(),
                    serde_json::json!({"reasoningEffort": effort}),
                );
            }
            entry.insert("variants".to_owned(), Value::Object(variants));
        }
    }
    Value::Object(entry)
}

/// V2 model entry: `name` / `limit` / `capabilities`.
///
/// `capabilities.tools` is emitted only when exactly known (`Some(true)` or
/// `Some(false)` from guaranteed function-tool support); unknown stays
/// omitted rather than guessed. No `modelID` override (the map key already
/// is the ID EggPool serves), no `transport` (HTTP default; EggPool has no
/// WebSocket path), no cost/family metadata.
fn v2_model_entry(projection: &AgentModelProjection) -> Value {
    let mut entry = Map::new();
    if let Some((key, value)) = display_name_entry(projection) {
        entry.insert(key, value);
    }
    if let Some(limit) = limit_object(projection) {
        entry.insert("limit".to_owned(), Value::Object(limit));
    }
    let mut input = vec![Value::String("text".to_owned())];
    if projection.capabilities.input_images == Some(true) {
        input.push(Value::String("image".to_owned()));
    }
    let mut capabilities = Map::from_iter([
        ("input".to_owned(), Value::Array(input)),
        (
            "output".to_owned(),
            Value::Array(vec![Value::String("text".to_owned())]),
        ),
    ]);
    if let Some(tools) = projection.capabilities.function_tools {
        capabilities.insert("tools".to_owned(), Value::Bool(tools));
    }
    entry.insert("capabilities".to_owned(), Value::Object(capabilities));
    Value::Object(entry)
}

fn sorted_projections(projections: &[AgentModelProjection]) -> Vec<AgentModelProjection> {
    let mut sorted = projections.to_vec();
    sorted.sort_by(|left, right| left.public_id.cmp(&right.public_id));
    sorted
}

// ---------------------------------------------------------------------------
// Expected provider values
// ---------------------------------------------------------------------------

/// Expected V1 `provider.eggpool` value.
pub fn expected_opencode_provider_v1(
    base_url: &str,
    projections: &[AgentModelProjection],
) -> Result<Value, ClientConfigError> {
    let mut models = BTreeMap::new();
    for projection in &sorted_projections(projections) {
        models.insert(projection.public_id.clone(), v1_model_entry(projection));
    }
    let options = Map::from_iter([
        ("baseURL".to_owned(), Value::String(base_url.to_owned())),
        (
            "apiKey".to_owned(),
            Value::String(format!("{{env:{EGGPOOL_API_KEY_ENV}}}")),
        ),
    ]);
    Ok(Value::Object(Map::from_iter([
        (
            "npm".to_owned(),
            Value::String(OPENCODE_RESPONSES_NPM.to_owned()),
        ),
        ("name".to_owned(), Value::String("EggPool".to_owned())),
        ("options".to_owned(), Value::Object(options)),
        ("models".to_owned(), serde_json::to_value(models)?),
    ])))
}

/// Expected V2 `providers.eggpool` value.
pub fn expected_opencode_provider_v2(
    base_url: &str,
    projections: &[AgentModelProjection],
) -> Result<Value, ClientConfigError> {
    let mut models = BTreeMap::new();
    for projection in &sorted_projections(projections) {
        models.insert(projection.public_id.clone(), v2_model_entry(projection));
    }
    let settings = Map::from_iter([("baseURL".to_owned(), Value::String(base_url.to_owned()))]);
    Ok(Value::Object(Map::from_iter([
        ("name".to_owned(), Value::String("EggPool".to_owned())),
        (
            "env".to_owned(),
            Value::Array(vec![Value::String(EGGPOOL_API_KEY_ENV.to_owned())]),
        ),
        (
            "package".to_owned(),
            Value::String(OPENCODE_V2_RESPONSES_PACKAGE.to_owned()),
        ),
        ("settings".to_owned(), Value::Object(settings)),
        ("models".to_owned(), serde_json::to_value(models)?),
    ])))
}

/// Expected owned provider value for an explicit variant.
pub fn expected_opencode_provider_for(
    variant: ClientSchemaVariant,
    base_url: &str,
    projections: &[AgentModelProjection],
) -> Result<Value, ClientConfigError> {
    match variant {
        ClientSchemaVariant::OpencodeV2 => expected_opencode_provider_v2(base_url, projections),
        _ => expected_opencode_provider_v1(base_url, projections),
    }
}

/// Extract the expected `provider.eggpool` value for drift checks (V1).
pub fn expected_opencode_provider(
    base_url: &str,
    projections: &[AgentModelProjection],
) -> Result<Value, ClientConfigError> {
    expected_opencode_provider_v1(base_url, projections)
}

// ---------------------------------------------------------------------------
// Full-document renderers (empty/new files)
// ---------------------------------------------------------------------------

/// Render a full V1 OpenCode JSON config from a base URL and projections.
pub fn render_opencode_config(
    base_url: &str,
    projections: &[AgentModelProjection],
) -> Result<String, ClientConfigError> {
    let eggpool = expected_opencode_provider_v1(base_url, projections)?;
    let provider = Map::from_iter([("eggpool".to_owned(), eggpool)]);
    let root = Map::from_iter([
        (
            "$schema".to_owned(),
            Value::String("https://opencode.ai/config.json".to_owned()),
        ),
        ("provider".to_owned(), Value::Object(provider)),
    ]);
    Ok(serde_json::to_string_pretty(&root)?)
}

/// Render a full V2 OpenCode JSON config from a base URL and projections.
pub fn render_opencode_config_v2(
    base_url: &str,
    projections: &[AgentModelProjection],
) -> Result<String, ClientConfigError> {
    let eggpool = expected_opencode_provider_v2(base_url, projections)?;
    let providers = Map::from_iter([("eggpool".to_owned(), eggpool)]);
    let root = Map::from_iter([
        (
            "$schema".to_owned(),
            Value::String("https://opencode.ai/config.json".to_owned()),
        ),
        ("providers".to_owned(), Value::Object(providers)),
    ]);
    let mut rendered = serde_json::to_string_pretty(&root)?;
    rendered.push('\n');
    Ok(rendered)
}

/// Render a full document for a variant (empty/new files only).
pub fn render_opencode_document(
    variant: ClientSchemaVariant,
    base_url: &str,
    projections: &[AgentModelProjection],
) -> Result<String, ClientConfigError> {
    match variant {
        ClientSchemaVariant::OpencodeV2 => render_opencode_config_v2(base_url, projections),
        _ => {
            let mut rendered = render_opencode_config(base_url, projections)?;
            rendered.push('\n');
            Ok(rendered)
        }
    }
}

// ---------------------------------------------------------------------------
// Variant selection
// ---------------------------------------------------------------------------

/// Default variant from client version evidence.
///
/// Shape evidence always wins (see [`select_opencode_variant`]); this is only
/// the fallback for empty configs or configs without either parent key.
/// Major line 2 and above selects V2; anything else (1.x, unknown, absent)
/// keeps the qualified V1 default so same-host `configsetup` behavior is
/// preserved and fresh installs stay installable. Post-write native
/// verification (`opencode models` must list EggPool) is the backstop when
/// version evidence is absent.
pub fn default_variant_for_version(version_raw: Option<&str>) -> ClientSchemaVariant {
    let major_two = version_raw
        .and_then(|raw| raw.trim().trim_start_matches(['v', 'V']).split('.').next())
        .and_then(|major| major.parse::<u64>().ok())
        .is_some_and(|major| major >= 2);
    if major_two {
        ClientSchemaVariant::OpencodeV2
    } else {
        ClientSchemaVariant::OpencodeV1
    }
}

/// Select the schema variant for `existing` config text.
///
/// Selection is shape-first: `providers` (plural) selects V2, `provider`
/// selects V1. Both present is ambiguous and fails closed. Neither present
/// (or an empty/comment-only document) falls back to
/// [`default_variant_for_version`]. Unparseable JSONC fails closed with a
/// bounded line/column location. Never writes both key families into one
/// file.
pub fn select_opencode_variant(
    existing: &str,
    version_raw: Option<&str>,
) -> Result<ClientSchemaVariant, ClientConfigError> {
    if existing.trim().is_empty() {
        return Ok(default_variant_for_version(version_raw));
    }
    let value = jsonc::parse_value(existing).map_err(|error| ClientConfigError::UnsafeRewrite {
        detail: error.detail(),
    })?;
    let object = value
        .as_object()
        .ok_or_else(|| ClientConfigError::UnsafeRewrite {
            detail: "existing OpenCode config root is not an object".to_owned(),
        })?;
    let has_v1 = object.contains_key(OPENCODE_V1_PARENT);
    let has_v2 = object.contains_key(OPENCODE_V2_PARENT);
    match (has_v1, has_v2) {
        (true, true) => Err(ClientConfigError::UnsupportedSchema {
            detail: "OpenCode config contains both provider and providers; refusing automatic mutation (edit manually or use plan output)".to_owned(),
        }),
        (false, true) => Ok(ClientSchemaVariant::OpencodeV2),
        (true, false) => Ok(ClientSchemaVariant::OpencodeV1),
        (false, false) => Ok(default_variant_for_version(version_raw)),
    }
}

// ---------------------------------------------------------------------------
// Trivia-preserving mutation
// ---------------------------------------------------------------------------

/// Result of a preserving OpenCode mutation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct OpencodeMutation {
    /// Proposed document bytes (caller decides whether to write them).
    pub text: String,
    /// Exact previous raw text of the owned entry, if one existed. Captured
    /// before mutation so `remove` can restore it byte-for-byte.
    pub previous_raw: Option<String>,
    /// Variant the mutation was planned for.
    pub variant: ClientSchemaVariant,
}

/// Re-indent a pretty-printed JSON block under `entry_indent`.
///
/// The first line stays on the `"eggpool": ` line; continuation lines gain
/// the entry's indentation prefix.
fn indent_block(rendered: &str, entry_indent: &str) -> String {
    let mut lines = rendered.lines();
    let Some(first) = lines.next() else {
        return "{}".to_owned();
    };
    let mut out = first.to_owned();
    for line in lines {
        out.push('\n');
        if !line.trim().is_empty() {
            out.push_str(entry_indent);
        }
        out.push_str(line);
    }
    out
}

fn render_block(value: &Value, entry_indent: &str) -> Result<String, ClientConfigError> {
    let pretty = serde_json::to_string_pretty(value)?;
    Ok(indent_block(&pretty, entry_indent))
}

/// Full document for empty input: `$schema` plus the variant parent holding
/// only the owned entry.
fn full_document_for(
    variant: ClientSchemaVariant,
    expected: &Value,
) -> Result<String, ClientConfigError> {
    let parent_key = parent_key_for(variant);
    let parent_value = Value::Object(Map::from_iter([(
        OPENCODE_OWNED_ENTRY.to_owned(),
        expected.clone(),
    )]));
    let parent_rendered = serde_json::to_string_pretty(&parent_value)?;
    let parent_block = indent_block(&parent_rendered, "  ");
    Ok(format!(
        "{{\n  \"$schema\": \"https://opencode.ai/config.json\",\n  \"{parent_key}\": {parent_block}\n}}\n"
    ))
}

/// Apply the owned provider entry to `existing`, preserving all other bytes.
///
/// - Empty input renders a full variant document (leading comments are kept
///   above the generated object).
/// - Otherwise only the `eggpool` entry under the variant parent is inserted
///   or replaced; the parent key is created when absent.
/// - Invalid JSONC or a non-object root/parent fails closed with a bounded
///   location; nothing is synthesized to "repair" user content.
pub fn apply_opencode_document(
    existing: &str,
    variant: ClientSchemaVariant,
    expected: &Value,
) -> Result<OpencodeMutation, ClientConfigError> {
    if existing.trim().is_empty() {
        if crate::text::has_jsonc_comments(existing) {
            let mut preserved = existing.to_owned();
            if !preserved.ends_with('\n') {
                preserved.push('\n');
            }
            let full = full_document_for(variant, expected)?;
            preserved.push_str(&full);
            jsonc::parse_value(&preserved).map_err(ClientConfigError::from)?;
            return Ok(OpencodeMutation {
                text: preserved,
                previous_raw: None,
                variant,
            });
        }
        return Ok(OpencodeMutation {
            text: full_document_for(variant, expected)?,
            previous_raw: None,
            variant,
        });
    }
    let doc = Doc::parse(existing).map_err(ClientConfigError::from)?;
    let root_entries = doc.root_entries().map_err(ClientConfigError::from)?;
    let text = doc.text();
    let parent_key = parent_key_for(variant);
    let Some(parent) = root_entries.iter().find(|entry| entry.key == parent_key) else {
        let proposed = insert_parent(text, &doc, &root_entries, parent_key, expected)?;
        return Ok(OpencodeMutation {
            text: proposed,
            previous_raw: None,
            variant,
        });
    };
    let children = doc
        .object_entries(parent.value_start, parent.value_end)
        .map_err(ClientConfigError::from)?
        .ok_or_else(|| ClientConfigError::UnsafeRewrite {
            detail: format!("existing OpenCode config has a non-object {parent_key:?} key"),
        })?;
    let Some(owned) = children
        .iter()
        .find(|entry| entry.key == OPENCODE_OWNED_ENTRY)
    else {
        return Ok(OpencodeMutation {
            text: insert_entry(text, &doc, parent, &children, expected)?,
            previous_raw: None,
            variant,
        });
    };
    let previous_raw = Some(text[owned.value_start..owned.value_end].to_owned());
    let entry_indent = jsonc::line_indent(text, owned.key_start);
    let block = render_block(expected, &entry_indent)?;
    let mut proposed = String::with_capacity(text.len() + block.len());
    proposed.push_str(&text[..owned.value_start]);
    proposed.push_str(&block);
    proposed.push_str(&text[owned.value_end..]);
    jsonc::parse_value(&proposed).map_err(|error| ClientConfigError::Drift {
        detail: format!("OpenCode splice produced invalid JSONC: {}", error.detail()),
    })?;
    Ok(OpencodeMutation {
        text: proposed,
        previous_raw,
        variant,
    })
}

/// Insert a missing parent key (`provider`/`providers` with the owned entry)
/// into the root object, preserving siblings and trivia.
fn insert_parent(
    text: &str,
    doc: &Doc<'_>,
    root_entries: &[jsonc::Entry],
    parent_key: &str,
    expected: &Value,
) -> Result<String, ClientConfigError> {
    let (root_open_end, root_close_start, _) = doc.root_span().map_err(ClientConfigError::from)?;
    let entry_indent = root_entries
        .last()
        .map(|entry| jsonc::line_indent(text, entry.key_start))
        .unwrap_or_else(|| "  ".to_owned());
    let parent_indent = jsonc::line_indent(text, root_close_start);
    let child_indent = format!("{entry_indent}  ");
    let owned_block = render_block(expected, &child_indent)?;
    let parent_text = format!(
        "\"{parent_key}\": {{\n{child_indent}\"{owned}\": {owned_block}\n{entry_indent}}}",
        owned = OPENCODE_OWNED_ENTRY,
    );
    splice_insert(
        text,
        doc,
        root_open_end,
        root_close_start,
        &parent_indent,
        &entry_indent,
        &parent_text,
    )
}

/// Insert a missing `eggpool` entry into an existing parent object.
fn insert_entry(
    text: &str,
    doc: &Doc<'_>,
    parent: &jsonc::Entry,
    children: &[jsonc::Entry],
    expected: &Value,
) -> Result<String, ClientConfigError> {
    let parent_indent = jsonc::line_indent(text, parent.key_start);
    let entry_indent = children
        .last()
        .map(|entry| jsonc::line_indent(text, entry.key_start))
        .unwrap_or_else(|| format!("{parent_indent}  "));
    let block = render_block(expected, &entry_indent)?;
    let entry_text = format!("\"{OPENCODE_OWNED_ENTRY}\": {block}");
    splice_insert(
        text,
        doc,
        parent.value_start + 1,
        parent.value_end - 1,
        &parent_indent,
        &entry_indent,
        &entry_text,
    )
}

/// Insert `payload` into the object spanning `(open_end, close_start)`.
///
/// - Empty, comment-free interiors gain newlines plus indentation.
/// - Otherwise the payload appends after the last significant token, adding
///   a comma only when the last token is not already a comma.
fn splice_insert(
    text: &str,
    doc: &Doc<'_>,
    open_end: usize,
    close_start: usize,
    parent_indent: &str,
    entry_indent: &str,
    payload: &str,
) -> Result<String, ClientConfigError> {
    let interior = doc.significant_spans(open_end, close_start);
    let has_comments = doc.has_comments_in(open_end, close_start);
    let (insert_at, separator) = if interior.is_empty() && !has_comments {
        (
            close_start,
            format!("\n{entry_indent}{payload}\n{parent_indent}"),
        )
    } else if let Some(last) = interior.last() {
        if last.is_comma {
            (last.end, format!("\n{entry_indent}{payload}"))
        } else {
            (last.end, format!(",\n{entry_indent}{payload}"))
        }
    } else {
        // Comments but no entries: append before the close without a comma
        // (comments are not values).
        (close_start, format!("\n{entry_indent}{payload}"))
    };
    let mut proposed = String::with_capacity(text.len() + separator.len());
    proposed.push_str(&text[..insert_at]);
    proposed.push_str(&separator);
    proposed.push_str(&text[insert_at..]);
    jsonc::parse_value(&proposed).map_err(|error| ClientConfigError::Drift {
        detail: format!("OpenCode splice produced invalid JSONC: {}", error.detail()),
    })?;
    Ok(proposed)
}

// ---------------------------------------------------------------------------
// Ownership-aware removal
// ---------------------------------------------------------------------------

/// Remove the owned provider entry, restoring `previous_raw` when present.
///
/// - `Some(previous_raw)`: splice the captured bytes back exactly (parsed
///   first so corrupt captures fail closed instead of writing garbage).
/// - `None`: remove only the owned entry plus one adjacent comma; an emptied
///   parent is removed only when it holds no comments or entries, otherwise
///   the empty parent stays (safer than touching trivia).
pub fn remove_opencode_document(
    existing: &str,
    variant: ClientSchemaVariant,
    previous_raw: Option<&str>,
) -> Result<String, ClientConfigError> {
    let parent_key = parent_key_for(variant);
    let doc = Doc::parse(existing).map_err(ClientConfigError::from)?;
    let root_entries = doc.root_entries().map_err(ClientConfigError::from)?;
    let text = doc.text();
    let parent = root_entries
        .iter()
        .find(|entry| entry.key == parent_key)
        .ok_or_else(|| ClientConfigError::Drift {
            detail: "no EggPool provider entry to remove".to_owned(),
        })?;
    let children = doc
        .object_entries(parent.value_start, parent.value_end)
        .map_err(ClientConfigError::from)?
        .ok_or_else(|| ClientConfigError::UnsafeRewrite {
            detail: format!("existing OpenCode config has a non-object {parent_key:?} key"),
        })?;
    let owned = children
        .iter()
        .find(|entry| entry.key == OPENCODE_OWNED_ENTRY)
        .ok_or_else(|| ClientConfigError::Drift {
            detail: "no EggPool provider entry to remove".to_owned(),
        })?;
    if let Some(previous) = previous_raw {
        jsonc::parse_value(previous).map_err(|_| ClientConfigError::Drift {
            detail: "captured previous OpenCode provider entry is not valid JSONC".to_owned(),
        })?;
        let mut proposed = String::with_capacity(text.len() + previous.len());
        proposed.push_str(&text[..owned.value_start]);
        proposed.push_str(previous);
        proposed.push_str(&text[owned.value_end..]);
        jsonc::parse_value(&proposed).map_err(|error| ClientConfigError::Drift {
            detail: format!(
                "OpenCode restore produced invalid JSONC: {}",
                error.detail()
            ),
        })?;
        return Ok(proposed);
    }
    remove_entry_and_maybe_parent(text, &doc, parent, &children, owned)
}

/// Remove one entry plus a single adjacent comma (trailing preferred).
fn remove_entry_and_maybe_parent(
    text: &str,
    doc: &Doc<'_>,
    parent: &jsonc::Entry,
    children: &[jsonc::Entry],
    owned: &jsonc::Entry,
) -> Result<String, ClientConfigError> {
    let after = doc.significant_spans(owned.value_end, parent.value_end);
    let before = doc.significant_spans(parent.value_start, owned.key_start);
    let (mut cut_start, mut cut_end) = (owned.key_start, owned.value_end);
    if after.first().is_some_and(|span| span.is_comma) {
        cut_end = after.first().map(|span| span.end).unwrap_or(cut_end);
    } else if before.last().is_some_and(|span| span.is_comma) {
        cut_start = before.last().map(|span| span.start).unwrap_or(cut_start);
    }
    let _ = children;
    let mut proposed = String::with_capacity(text.len());
    proposed.push_str(&text[..cut_start]);
    proposed.push_str(&text[cut_end..]);
    // If the parent now holds no entries and no comments, remove the parent
    // key as well (same comma rule one level up).
    let reparsed = Doc::parse(&proposed).map_err(|error| ClientConfigError::Drift {
        detail: format!(
            "OpenCode removal produced invalid JSONC: {}",
            error.detail()
        ),
    })?;
    let re_root = reparsed
        .root_entries()
        .map_err(|error| ClientConfigError::Drift {
            detail: format!(
                "OpenCode removal produced invalid JSONC: {}",
                error.detail()
            ),
        })?;
    let parent_key = parent.key.clone();
    if let Some(re_parent) = re_root.iter().find(|entry| entry.key == parent_key) {
        let re_children = reparsed
            .object_entries(re_parent.value_start, re_parent.value_end)
            .map_err(|error| ClientConfigError::Drift {
                detail: format!(
                    "OpenCode removal produced invalid JSONC: {}",
                    error.detail()
                ),
            })?
            .unwrap_or_default();
        if re_children.is_empty()
            && !reparsed.has_comments_in(re_parent.value_start, re_parent.value_end)
        {
            return remove_parent_key(reparsed.text(), &reparsed, &re_root, re_parent);
        }
    }
    jsonc::parse_value(&proposed).map_err(|error| ClientConfigError::Drift {
        detail: format!(
            "OpenCode removal produced invalid JSONC: {}",
            error.detail()
        ),
    })?;
    Ok(proposed)
}

/// Remove a whole parent key plus one adjacent comma.
fn remove_parent_key(
    text: &str,
    doc: &Doc<'_>,
    root_entries: &[jsonc::Entry],
    parent: &jsonc::Entry,
) -> Result<String, ClientConfigError> {
    let (_, root_close_start, _) = doc.root_span().map_err(ClientConfigError::from)?;
    let after = doc.significant_spans(parent.value_end, root_close_start);
    let (mut cut_start, mut cut_end) = (parent.key_start, parent.value_end);
    if after.first().is_some_and(|span| span.is_comma) {
        cut_end = after.first().map(|span| span.end).unwrap_or(cut_end);
    } else if let Some(index) = root_entries
        .iter()
        .position(|entry| entry.key == parent.key)
    {
        if index > 0 {
            let prev = &root_entries[index - 1];
            let between = doc.significant_spans(prev.value_end, parent.key_start);
            if let Some(comma) = between.iter().find(|span| span.is_comma) {
                cut_start = comma.start;
            }
        }
    }
    let mut proposed = String::with_capacity(text.len());
    proposed.push_str(&text[..cut_start]);
    proposed.push_str(&text[cut_end..]);
    jsonc::parse_value(&proposed).map_err(|error| ClientConfigError::Drift {
        detail: format!(
            "OpenCode removal produced invalid JSONC: {}",
            error.detail()
        ),
    })?;
    Ok(proposed)
}

// ---------------------------------------------------------------------------
// Inspection, drift, and sync policy
// ---------------------------------------------------------------------------

/// Read the current owned provider entry from a parsed document.
pub fn current_owned_entry(document: &Value, variant: ClientSchemaVariant) -> Option<&Value> {
    document
        .get(parent_key_for(variant))
        .and_then(|parent| parent.get(OPENCODE_OWNED_ENTRY))
}

/// True when `current` and `desired` agree on runtime/auth/endpoint identity
/// (package/npm, display name, and full options/settings). Model-list-only
/// differences are a safe revision refresh, not drift.
pub fn owned_entry_allows_sync(
    current: &Value,
    desired: &Value,
    variant: ClientSchemaVariant,
) -> bool {
    let (Some(current_object), Some(desired_object)) = (current.as_object(), desired.as_object())
    else {
        return false;
    };
    match variant {
        ClientSchemaVariant::OpencodeV2 => {
            current_object.get("package") == desired_object.get("package")
                && current_object.get("name") == desired_object.get("name")
                && current_object.get("env") == desired_object.get("env")
                && current_object.get("settings") == desired_object.get("settings")
        }
        _ => {
            current_object.get("npm") == desired_object.get("npm")
                && current_object.get("name") == desired_object.get("name")
                && current_object.get("options") == desired_object.get("options")
        }
    }
}

/// True when the owned entry looks like EggPool's (Responses runtime plus
/// the `EGGPOOL_API_KEY` reference), used to distinguish our install from
/// hand-written drift when no previous capture exists.
pub fn looks_like_eggpool_entry(value: &Value, variant: ClientSchemaVariant) -> bool {
    let rendered = value.to_string();
    if !rendered.contains(EGGPOOL_API_KEY_ENV) {
        return false;
    }
    match variant {
        ClientSchemaVariant::OpencodeV2 => value
            .get("package")
            .and_then(Value::as_str)
            .is_some_and(|package| package.contains("openai")),
        _ => value
            .get("npm")
            .and_then(Value::as_str)
            .is_some_and(|npm| npm.contains("openai")),
    }
}

/// Capture the exact raw text of the owned provider entry, if present.
///
/// Returns `(owned_path, raw_text)` for manifest/backup storage so removal
/// can restore it byte-for-byte.
pub fn capture_owned_raw(
    existing: &str,
    variant: ClientSchemaVariant,
) -> Result<Option<(String, String)>, ClientConfigError> {
    if existing.trim().is_empty() {
        return Ok(None);
    }
    let doc = Doc::parse(existing).map_err(ClientConfigError::from)?;
    let root_entries = doc.root_entries().map_err(ClientConfigError::from)?;
    let parent_key = parent_key_for(variant);
    let Some(parent) = root_entries.iter().find(|entry| entry.key == parent_key) else {
        return Ok(None);
    };
    let children = doc
        .object_entries(parent.value_start, parent.value_end)
        .map_err(ClientConfigError::from)?
        .unwrap_or_default();
    Ok(children
        .iter()
        .find(|entry| entry.key == OPENCODE_OWNED_ENTRY)
        .map(|owned| {
            (
                owned_path_for(variant).to_owned(),
                doc.text()[owned.value_start..owned.value_end].to_owned(),
            )
        }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::projection::{project_models, IntegrationModel, ModelLimits};
    use serde_json::{Map, Value};

    fn projections() -> Vec<AgentModelProjection> {
        let models = vec![IntegrationModel {
            model_id: "gpt-4o/openai".to_owned(),
            base_model_id: "gpt-4o".to_owned(),
            provider_id: Some("openai".to_owned()),
            display_name: "GPT-4o".to_owned(),
            capabilities: Value::Object(Map::new()),
            source_metadata: Value::Object(Map::new()),
            limits: ModelLimits {
                context_tokens: Some(128_000),
                ..Default::default()
            },
        }];
        project_models(&models)
    }

    #[test]
    fn opencode_renderer_uses_responses_runtime_and_env_key() {
        let rendered = render_opencode_config("http://192.168.1.100:11300/v1", &projections())
            .expect("opencode");
        assert!(rendered.contains(OPENCODE_RESPONSES_NPM));
        assert!(!rendered.contains("@ai-sdk/openai-compatible"));
        assert!(rendered.contains("{env:EGGPOOL_API_KEY}"));
        let value: Value = serde_json::from_str(&rendered).expect("json");
        let provider = value
            .get("provider")
            .and_then(|provider| provider.get("eggpool"))
            .expect("eggpool provider");
        assert_eq!(
            provider.get("npm").and_then(Value::as_str),
            Some(OPENCODE_RESPONSES_NPM)
        );
        let models = provider
            .get("models")
            .and_then(Value::as_object)
            .expect("models");
        let entry = models.get("gpt-4o/openai").expect("model entry");
        assert_eq!(
            entry
                .get("limit")
                .and_then(|limit| limit.get("context"))
                .and_then(Value::as_u64),
            Some(128_000)
        );
    }

    #[test]
    fn opencode_renderer_is_secret_free_and_deterministic() {
        let first =
            render_opencode_config("http://127.0.0.1:11300/v1", &projections()).expect("first");
        let second =
            render_opencode_config("http://127.0.0.1:11300/v1", &projections()).expect("second");
        assert_eq!(first, second);
        assert!(!first.contains("ep_test_key"));
    }

    #[test]
    fn v2_renderer_uses_responses_package_and_env_list() {
        let rendered =
            render_opencode_config_v2("https://pool.example/v1", &projections()).expect("v2");
        let value: Value = serde_json::from_str(&rendered).expect("json");
        let provider = value
            .get("providers")
            .and_then(|providers| providers.get("eggpool"))
            .expect("eggpool provider");
        assert_eq!(
            provider.get("package").and_then(Value::as_str),
            Some(OPENCODE_V2_RESPONSES_PACKAGE)
        );
        assert_eq!(
            provider.get("env"),
            Some(&serde_json::json!(["EGGPOOL_API_KEY"]))
        );
        assert_eq!(
            provider
                .get("settings")
                .and_then(|settings| settings.get("baseURL"))
                .and_then(Value::as_str),
            Some("https://pool.example/v1")
        );
        // No interpolated apiKey and no chat-compat downgrade.
        assert!(!rendered.contains("{env:"));
        assert!(provider.get("transport").is_none());
        let models = provider
            .get("models")
            .and_then(Value::as_object)
            .expect("models");
        let entry = models.get("gpt-4o/openai").expect("model entry");
        assert_eq!(
            entry
                .get("limit")
                .and_then(|limit| limit.get("context"))
                .and_then(Value::as_u64),
            Some(128_000)
        );
        let capabilities = entry.get("capabilities").expect("capabilities");
        assert_eq!(
            capabilities
                .get("output")
                .and_then(Value::as_array)
                .map(Vec::len),
            Some(1)
        );
        // Unknown tool support stays omitted, never guessed.
        assert!(capabilities.get("tools").is_none());
    }

    #[test]
    fn variant_selection_is_shape_first_and_fails_closed() {
        assert_eq!(
            select_opencode_variant("", None).expect("empty"),
            ClientSchemaVariant::OpencodeV1
        );
        assert_eq!(
            select_opencode_variant("", Some("2.4.0")).expect("empty v2"),
            ClientSchemaVariant::OpencodeV2
        );
        assert_eq!(
            select_opencode_variant("{\"provider\": {}}", Some("2.4.0")).expect("shape wins"),
            ClientSchemaVariant::OpencodeV1
        );
        assert_eq!(
            select_opencode_variant("{\"providers\": {}}", None).expect("v2 shape"),
            ClientSchemaVariant::OpencodeV2
        );
        assert!(select_opencode_variant("{\"provider\": {}, \"providers\": {}}", None).is_err());
        assert!(select_opencode_variant("{\"provider\": ", None).is_err());
    }

    #[test]
    fn jsonc_mutation_preserves_comments_and_unrelated_keys() {
        let existing = "{\n  // user comment\n  \"$schema\": \"https://opencode.ai/config.json\",\n  \"theme\": \"dark\", /* trailing */\n  \"provider\": {\n    // provider note\n    \"other\": {\n      \"npm\": \"@other/pkg\"\n    }\n  }\n}\n";
        let expected = expected_opencode_provider_v1("https://pool.example/v1", &projections())
            .expect("expected");
        let mutation =
            apply_opencode_document(existing, ClientSchemaVariant::OpencodeV1, &expected)
                .expect("apply");
        assert!(mutation.previous_raw.is_none());
        assert!(mutation.text.contains("// user comment"));
        assert!(mutation.text.contains("// provider note"));
        assert!(mutation.text.contains("/* trailing */"));
        assert!(mutation.text.contains("\"theme\": \"dark\""));
        assert!(mutation.text.contains("\"other\""));
        let value: Value = jsonc::parse_value(&mutation.text).expect("parses");
        assert_eq!(
            value
                .get("provider")
                .and_then(|provider| provider.get("eggpool")),
            Some(&expected)
        );
    }

    #[test]
    fn jsonc_mutation_replaces_existing_entry_and_captures_previous() {
        let existing =
            "{\n  \"provider\": {\n    \"eggpool\": {\n      \"npm\": \"@old/pkg\"\n    }\n  }\n}\n";
        let expected = expected_opencode_provider_v1("https://pool.example/v1", &projections())
            .expect("expected");
        let mutation =
            apply_opencode_document(existing, ClientSchemaVariant::OpencodeV1, &expected)
                .expect("apply");
        let previous = mutation.previous_raw.expect("previous captured");
        assert!(previous.contains("@old/pkg"));
        let value: Value = jsonc::parse_value(&mutation.text).expect("parses");
        assert_eq!(
            value
                .get("provider")
                .and_then(|provider| provider.get("eggpool")),
            Some(&expected)
        );
    }

    #[test]
    fn jsonc_mutation_handles_trailing_commas_and_block_comments() {
        let existing = "{\n  \"provider\": {\n    \"other\": {},\n  },\n  /* end */\n}\n";
        let expected = expected_opencode_provider_v1("https://pool.example/v1", &projections())
            .expect("expected");
        let mutation =
            apply_opencode_document(existing, ClientSchemaVariant::OpencodeV1, &expected)
                .expect("apply");
        assert!(mutation.text.contains("/* end */"));
        let value: Value = jsonc::parse_value(&mutation.text).expect("parses");
        assert!(value
            .get("provider")
            .and_then(|provider| provider.get("eggpool"))
            .is_some());
        assert!(value
            .get("provider")
            .and_then(|provider| provider.get("other"))
            .is_some());
    }

    #[test]
    fn v2_mutation_creates_parent_without_touching_other_keys() {
        let existing = "{\n  \"custom\": true,\n}\n";
        let expected = expected_opencode_provider_v2("https://pool.example/v1", &projections())
            .expect("expected");
        let mutation =
            apply_opencode_document(existing, ClientSchemaVariant::OpencodeV2, &expected)
                .expect("apply");
        assert!(mutation.text.contains("\"custom\": true"));
        assert!(!mutation.text.contains("\"provider\":"));
        let value: Value = jsonc::parse_value(&mutation.text).expect("parses");
        assert!(value
            .get("providers")
            .and_then(|providers| providers.get("eggpool"))
            .is_some());
    }

    #[test]
    fn removal_restores_captured_previous_byte_for_byte() {
        let previous_raw = "{\n      \"npm\": \"@old/pkg\", // kept\n    }";
        let installed = format!(
            "{{\n  \"provider\": {{\n    \"eggpool\": {previous_raw},\n    \"other\": {{}}\n  }}\n}}\n"
        );
        let restored = remove_opencode_document(
            &installed,
            ClientSchemaVariant::OpencodeV1,
            Some(previous_raw),
        )
        .expect("remove");
        assert!(restored.contains(previous_raw));
        assert!(restored.contains("\"other\""));
    }

    #[test]
    fn removal_drops_only_the_owned_entry_and_empty_parent() {
        let expected = expected_opencode_provider_v1("https://pool.example/v1", &projections())
            .expect("expected");
        let mutation =
            apply_opencode_document("", ClientSchemaVariant::OpencodeV1, &expected).expect("apply");
        let removed =
            remove_opencode_document(&mutation.text, ClientSchemaVariant::OpencodeV1, None)
                .expect("remove");
        assert!(!removed.contains("eggpool"));
        assert!(!removed.contains("\"provider\""));
        assert!(removed.contains("$schema"));
    }

    #[test]
    fn removal_keeps_parent_when_comments_remain_inside() {
        let installed =
            "{\n  \"provider\": {\n    \"eggpool\": {\"npm\": \"x\"} // owned\n  }\n}\n";
        let removed = remove_opencode_document(installed, ClientSchemaVariant::OpencodeV1, None)
            .expect("remove");
        assert!(!removed.contains("\"eggpool\""));
        // The trailing comment is user trivia: the emptied parent stays
        // rather than discarding it.
        assert!(removed.contains("\"provider\""));
        assert!(removed.contains("// owned"));
    }

    #[test]
    fn sync_policy_distinguishes_revision_refresh_from_drift() {
        let base = "https://pool.example/v1";
        let first = expected_opencode_provider_v1(base, &projections()).expect("first");
        assert!(owned_entry_allows_sync(
            &first,
            &first,
            ClientSchemaVariant::OpencodeV1
        ));
        let other = expected_opencode_provider_v1(base, &[]).expect("other");
        assert!(owned_entry_allows_sync(
            &first,
            &other,
            ClientSchemaVariant::OpencodeV1
        ));
        let moved = expected_opencode_provider_v1("https://other.example/v1", &projections())
            .expect("moved");
        assert!(!owned_entry_allows_sync(
            &first,
            &moved,
            ClientSchemaVariant::OpencodeV1
        ));
        let mut edited = first.clone();
        edited
            .as_object_mut()
            .expect("object")
            .insert("npm".to_owned(), Value::String("@evil/pkg".to_owned()));
        assert!(!owned_entry_allows_sync(
            &edited,
            &first,
            ClientSchemaVariant::OpencodeV1
        ));
    }

    #[test]
    fn malformed_jsonc_fails_closed_with_location() {
        let error = apply_opencode_document(
            "{\"provider\": }",
            ClientSchemaVariant::OpencodeV1,
            &Value::Null,
        )
        .expect_err("must fail");
        assert!(error.to_string().contains("line"));
    }
}
