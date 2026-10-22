//! OpenCode Go surface, authentication, and model-to-wire fixtures.
//!
//! Evidence source: the first-party OpenCode Go endpoint table at
//! `https://opencode.ai/docs/go` (canonical page
//! `https://opencode.ai/v2/docs/console/go/`), re-reviewed 2026-10-22. The
//! reviewed record below transcribes that page's "Endpoints" section: each row
//! is one exact documented model id, the wire-surface family the first-party
//! documentation places it on, and the endpoint URL the table publishes for it.
//!
//! Every row also records where its placement came from. A row the current
//! table no longer places is carried forward from the 2026-10-07 review
//! instead of being dropped: a placement that is not restated is not a
//! retraction, and a stale hint costs a consumer nothing. Carrying it forward
//! is what keeps the two-direction equality assertion between the hints and
//! this record meaningful instead of forcing a choice between deleting
//! evidence and breaking the guard.
//!
//! Two sets are deliberately unresolved and are recorded separately, because
//! neither can be expressed as a hint:
//!
//! * `gemini-4-argon-contributor`, which the current table places on a Google
//!   Generative AI surface family the shared contract does not model; and
//! * the catalog models the current table does not place at all.
//!
//! The live catalog (`https://opencode.ai/zen/go/v1/models`) is public — it
//! answered `200` with an ordinary OpenAI-shaped model list when read without
//! a credential on 2026-10-22 — so it is recorded as discovery evidence only
//! and never as credential proof. Its dated snapshot lives beside this
//! fixture, and a manual, non-CI refresh path rewrites it.

use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::path::{Path, PathBuf};

use eggpool_provider_profile::{ProviderAuthMode, ProviderProfileRegistry, WireSurface};

/// The endpoints the current first-party table publishes, per surface family.
const RESPONSES_URL: &str = "https://opencode.ai/inference/go/openai/v1/responses";
const CHAT_URL: &str = "https://opencode.ai/inference/go/openai/v1/chat/completions";
const MESSAGES_URL: &str = "https://opencode.ai/inference/go/anthropic/v1/messages";

/// The dispatch URLs this profile composes from its own base URL and its
/// per-surface path templates.
///
/// The first-party table publishes a different URL family. Both are live, and
/// the endpoint URL metadata is a separate fact from the model-to-surface
/// mapping this milestone changes.
const PROFILE_RESPONSES_URL: &str = "https://opencode.ai/zen/go/v1/responses";
const PROFILE_CHAT_URL: &str = "https://opencode.ai/zen/go/v1/chat/completions";
const PROFILE_MESSAGES_URL: &str = "https://opencode.ai/zen/go/v1/messages";

/// The dated catalog snapshot committed beside this fixture.
const CATALOG_SNAPSHOT_PATH: &str = "review/opencode_go_catalog_snapshot.json";

/// Where a reviewed row's placement came from.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Provenance {
    /// The current (2026-10-22) first-party endpoint table places this row.
    CurrentTable,
    /// The current table no longer places this row; the placement is carried
    /// forward from the 2026-10-07 review and has not been superseded.
    CarriedForward,
}

/// The live catalog's view of the model at the snapshot date.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum CatalogState {
    /// The dated catalog snapshot lists this model id.
    Listed,
    /// The dated catalog snapshot does not list this model id: the upstream no
    /// longer serves it.
    Withdrawn,
}

/// One reviewed row of the first-party OpenCode Go endpoint table.
#[derive(Debug, Clone, Copy)]
struct ReviewedRow {
    /// The exact documented model id.
    model_id: &'static str,
    /// The wire-surface family the first-party documentation places it on.
    surface: WireSurface,
    /// The endpoint the current first-party table publishes for this row.
    ///
    /// `None` for a carried-forward row: the current table publishes no
    /// endpoint for a model it no longer places.
    endpoint_url: Option<&'static str>,
    /// Where the placement came from.
    provenance: Provenance,
    /// The live catalog's view of the model at the snapshot date.
    catalog_state: CatalogState,
    /// Why this row carries the disposition it carries.
    reason: &'static str,
}

/// A model that is deliberately unresolved, with the reason it is unresolved.
#[derive(Debug, Clone, Copy)]
struct UnresolvedModel {
    /// The exact model id.
    model_id: &'static str,
    /// The endpoint the current first-party table publishes for it, when the
    /// table places it on a family the shared contract does not model.
    endpoint_url: Option<&'static str>,
    /// Why the model stays unresolved.
    reason: &'static str,
}

/// The row reason for a model the current table still places.
const PLACED_BY_CURRENT_TABLE: &str =
    "the 2026-10-22 first-party endpoint table places it on this endpoint family";

/// The row reason for a model the current table places and the 2026-10-07
/// review did not document.
const PLACED_BY_CURRENT_TABLE_NEW: &str = "the 2026-10-22 first-party endpoint table places it here; the 2026-10-07 review did not document it";

/// The row reason for a model the current table no longer places while the
/// live catalog still lists it.
const CARRIED_FORWARD_STILL_LISTED: &str = "the 2026-10-22 table no longer places it and the live catalog still lists it, so the 2026-10-07 placement is carried forward rather than retracted";

/// The row reason for a model the current table no longer places and the live
/// catalog no longer lists.
const CARRIED_FORWARD_WITHDRAWN: &str = "the 2026-10-22 table no longer places it and the live catalog no longer lists it; the 2026-10-07 placement is carried forward because a stale hint costs a consumer nothing";

/// The row reason for a model the current table still places but whose
/// upstream withdrew.
const PLACED_BUT_WITHDRAWN_UPSTREAM: &str = "the 2026-10-22 table still places it, but the live catalog no longer lists it: the upstream withdrew the model, so the hint is retained";

/// The complete reviewed OpenCode Go endpoint table.
///
/// The twenty-eight rows placed by the current table come first, in the order
/// the first-party page lists them; the six rows the current table no longer
/// places follow, each carried forward from the 2026-10-07 review.
const REVIEWED_TABLE: &[ReviewedRow] = &[
    // Responses
    ReviewedRow {
        model_id: "grok-4.7",
        surface: WireSurface::OpenaiResponses,
        endpoint_url: Some(RESPONSES_URL),
        provenance: Provenance::CurrentTable,
        catalog_state: CatalogState::Listed,
        reason: PLACED_BY_CURRENT_TABLE,
    },
    ReviewedRow {
        model_id: "gpt-6-luna",
        surface: WireSurface::OpenaiResponses,
        endpoint_url: Some(RESPONSES_URL),
        provenance: Provenance::CurrentTable,
        catalog_state: CatalogState::Listed,
        reason: PLACED_BY_CURRENT_TABLE,
    },
    ReviewedRow {
        model_id: "gpt-5.6-luna",
        surface: WireSurface::OpenaiResponses,
        endpoint_url: Some(RESPONSES_URL),
        provenance: Provenance::CurrentTable,
        catalog_state: CatalogState::Listed,
        reason: PLACED_BY_CURRENT_TABLE,
    },
    ReviewedRow {
        model_id: "muse-spark-1.3-contributor",
        surface: WireSurface::OpenaiResponses,
        endpoint_url: Some(RESPONSES_URL),
        provenance: Provenance::CurrentTable,
        catalog_state: CatalogState::Listed,
        reason: PLACED_BY_CURRENT_TABLE,
    },
    ReviewedRow {
        model_id: "muse-spark-1.2-contributor",
        surface: WireSurface::OpenaiResponses,
        endpoint_url: Some(RESPONSES_URL),
        provenance: Provenance::CurrentTable,
        catalog_state: CatalogState::Listed,
        reason: PLACED_BY_CURRENT_TABLE,
    },
    // Anthropic Messages
    ReviewedRow {
        model_id: "claude-haiku-5-5",
        surface: WireSurface::AnthropicMessages,
        endpoint_url: Some(MESSAGES_URL),
        provenance: Provenance::CurrentTable,
        catalog_state: CatalogState::Listed,
        reason: PLACED_BY_CURRENT_TABLE_NEW,
    },
    ReviewedRow {
        model_id: "minimax-m3",
        surface: WireSurface::AnthropicMessages,
        endpoint_url: Some(MESSAGES_URL),
        provenance: Provenance::CurrentTable,
        catalog_state: CatalogState::Listed,
        reason: PLACED_BY_CURRENT_TABLE,
    },
    ReviewedRow {
        model_id: "qwen3.8-max",
        surface: WireSurface::AnthropicMessages,
        endpoint_url: Some(MESSAGES_URL),
        provenance: Provenance::CurrentTable,
        catalog_state: CatalogState::Listed,
        reason: PLACED_BY_CURRENT_TABLE,
    },
    ReviewedRow {
        model_id: "qwen3.8-flash",
        surface: WireSurface::AnthropicMessages,
        endpoint_url: Some(MESSAGES_URL),
        provenance: Provenance::CurrentTable,
        catalog_state: CatalogState::Listed,
        reason: PLACED_BY_CURRENT_TABLE,
    },
    ReviewedRow {
        model_id: "qwen3.7-plus",
        surface: WireSurface::AnthropicMessages,
        endpoint_url: Some(MESSAGES_URL),
        provenance: Provenance::CurrentTable,
        catalog_state: CatalogState::Listed,
        reason: PLACED_BY_CURRENT_TABLE,
    },
    // Chat Completions
    ReviewedRow {
        model_id: "glm-5.3-flash",
        surface: WireSurface::OpenaiChatCompletions,
        endpoint_url: Some(CHAT_URL),
        provenance: Provenance::CurrentTable,
        catalog_state: CatalogState::Listed,
        reason: PLACED_BY_CURRENT_TABLE,
    },
    ReviewedRow {
        model_id: "glm-5.3",
        surface: WireSurface::OpenaiChatCompletions,
        endpoint_url: Some(CHAT_URL),
        provenance: Provenance::CurrentTable,
        catalog_state: CatalogState::Listed,
        reason: PLACED_BY_CURRENT_TABLE,
    },
    ReviewedRow {
        model_id: "glm-5.2",
        surface: WireSurface::OpenaiChatCompletions,
        endpoint_url: Some(CHAT_URL),
        provenance: Provenance::CurrentTable,
        catalog_state: CatalogState::Listed,
        reason: PLACED_BY_CURRENT_TABLE,
    },
    ReviewedRow {
        model_id: "kimi-k3",
        surface: WireSurface::OpenaiChatCompletions,
        endpoint_url: Some(CHAT_URL),
        provenance: Provenance::CurrentTable,
        catalog_state: CatalogState::Listed,
        reason: PLACED_BY_CURRENT_TABLE,
    },
    ReviewedRow {
        model_id: "kimi-k2.7-code",
        surface: WireSurface::OpenaiChatCompletions,
        endpoint_url: Some(CHAT_URL),
        provenance: Provenance::CurrentTable,
        catalog_state: CatalogState::Listed,
        reason: PLACED_BY_CURRENT_TABLE,
    },
    ReviewedRow {
        model_id: "longcat-2.0",
        surface: WireSurface::OpenaiChatCompletions,
        endpoint_url: Some(CHAT_URL),
        provenance: Provenance::CurrentTable,
        catalog_state: CatalogState::Listed,
        reason: PLACED_BY_CURRENT_TABLE,
    },
    ReviewedRow {
        model_id: "deepseek-v4.1-flash",
        surface: WireSurface::OpenaiChatCompletions,
        endpoint_url: Some(CHAT_URL),
        provenance: Provenance::CurrentTable,
        catalog_state: CatalogState::Listed,
        reason: PLACED_BY_CURRENT_TABLE,
    },
    ReviewedRow {
        model_id: "deepseek-v4-pro",
        surface: WireSurface::OpenaiChatCompletions,
        endpoint_url: Some(CHAT_URL),
        provenance: Provenance::CurrentTable,
        catalog_state: CatalogState::Listed,
        reason: PLACED_BY_CURRENT_TABLE,
    },
    ReviewedRow {
        model_id: "mimo-v2.6-flash",
        surface: WireSurface::OpenaiChatCompletions,
        endpoint_url: Some(CHAT_URL),
        provenance: Provenance::CurrentTable,
        catalog_state: CatalogState::Listed,
        reason: PLACED_BY_CURRENT_TABLE,
    },
    ReviewedRow {
        model_id: "mimo-v2.6-pro",
        surface: WireSurface::OpenaiChatCompletions,
        endpoint_url: Some(CHAT_URL),
        provenance: Provenance::CurrentTable,
        catalog_state: CatalogState::Listed,
        reason: PLACED_BY_CURRENT_TABLE,
    },
    ReviewedRow {
        model_id: "mimo-v2.5",
        surface: WireSurface::OpenaiChatCompletions,
        endpoint_url: Some(CHAT_URL),
        provenance: Provenance::CurrentTable,
        catalog_state: CatalogState::Listed,
        reason: PLACED_BY_CURRENT_TABLE,
    },
    ReviewedRow {
        model_id: "mimo-v2.5-pro",
        surface: WireSurface::OpenaiChatCompletions,
        endpoint_url: Some(CHAT_URL),
        provenance: Provenance::CurrentTable,
        catalog_state: CatalogState::Listed,
        reason: PLACED_BY_CURRENT_TABLE,
    },
    ReviewedRow {
        model_id: "hy4-preview",
        surface: WireSurface::OpenaiChatCompletions,
        endpoint_url: Some(CHAT_URL),
        provenance: Provenance::CurrentTable,
        catalog_state: CatalogState::Listed,
        reason: PLACED_BY_CURRENT_TABLE,
    },
    ReviewedRow {
        model_id: "hy3",
        surface: WireSurface::OpenaiChatCompletions,
        endpoint_url: Some(CHAT_URL),
        provenance: Provenance::CurrentTable,
        catalog_state: CatalogState::Listed,
        reason: PLACED_BY_CURRENT_TABLE,
    },
    ReviewedRow {
        model_id: "minimax-m3.1-flash-free",
        surface: WireSurface::OpenaiChatCompletions,
        endpoint_url: Some(CHAT_URL),
        provenance: Provenance::CurrentTable,
        catalog_state: CatalogState::Listed,
        reason: PLACED_BY_CURRENT_TABLE_NEW,
    },
    ReviewedRow {
        model_id: "longcat-2.5-preview-free",
        surface: WireSurface::OpenaiChatCompletions,
        endpoint_url: Some(CHAT_URL),
        provenance: Provenance::CurrentTable,
        catalog_state: CatalogState::Withdrawn,
        reason: PLACED_BUT_WITHDRAWN_UPSTREAM,
    },
    ReviewedRow {
        model_id: "step-5-preview",
        surface: WireSurface::OpenaiChatCompletions,
        endpoint_url: Some(CHAT_URL),
        provenance: Provenance::CurrentTable,
        catalog_state: CatalogState::Listed,
        reason: PLACED_BY_CURRENT_TABLE_NEW,
    },
    ReviewedRow {
        model_id: "beam-free",
        surface: WireSurface::OpenaiChatCompletions,
        endpoint_url: Some(CHAT_URL),
        provenance: Provenance::CurrentTable,
        catalog_state: CatalogState::Listed,
        reason: PLACED_BY_CURRENT_TABLE_NEW,
    },
    // Carried forward from the 2026-10-07 review: the current table no longer
    // places these rows.
    ReviewedRow {
        model_id: "grok-4.6",
        surface: WireSurface::OpenaiResponses,
        endpoint_url: None,
        provenance: Provenance::CarriedForward,
        catalog_state: CatalogState::Listed,
        reason: CARRIED_FORWARD_STILL_LISTED,
    },
    ReviewedRow {
        model_id: "kimi-k2.6",
        surface: WireSurface::OpenaiChatCompletions,
        endpoint_url: None,
        provenance: Provenance::CarriedForward,
        catalog_state: CatalogState::Listed,
        reason: CARRIED_FORWARD_STILL_LISTED,
    },
    ReviewedRow {
        model_id: "minimax-m2.7",
        surface: WireSurface::AnthropicMessages,
        endpoint_url: None,
        provenance: Provenance::CarriedForward,
        catalog_state: CatalogState::Listed,
        reason: CARRIED_FORWARD_STILL_LISTED,
    },
    ReviewedRow {
        model_id: "deepseek-v4-flash",
        surface: WireSurface::OpenaiChatCompletions,
        endpoint_url: None,
        provenance: Provenance::CarriedForward,
        catalog_state: CatalogState::Withdrawn,
        reason: CARRIED_FORWARD_WITHDRAWN,
    },
    ReviewedRow {
        model_id: "deepseek-v4-flash-vision-exp",
        surface: WireSurface::OpenaiChatCompletions,
        endpoint_url: None,
        provenance: Provenance::CarriedForward,
        catalog_state: CatalogState::Withdrawn,
        reason: CARRIED_FORWARD_WITHDRAWN,
    },
    ReviewedRow {
        model_id: "space-bunny",
        surface: WireSurface::OpenaiChatCompletions,
        endpoint_url: None,
        provenance: Provenance::CarriedForward,
        catalog_state: CatalogState::Withdrawn,
        reason: CARRIED_FORWARD_WITHDRAWN,
    },
];

/// The model the current first-party table places on a surface family the
/// shared contract does not model.
///
/// Adding a Google Generative AI surface would change `eggpool-wire`'s
/// surface vocabulary and codec ownership, so this milestone records the
/// finding and leaves the model unresolved. Mapping it onto an existing
/// surface would be a **wrong** hint: a consumer would dispatch a
/// Google-native model to an OpenAI-compatible endpoint.
const UNMODELLED_PLACEMENT: &[UnresolvedModel] = &[UnresolvedModel {
    model_id: "gemini-4-argon-contributor",
    endpoint_url: Some(
        "https://opencode.ai/inference/go/google/v1beta/models/gemini-4-argon-contributor",
    ),
    reason: "the 2026-10-22 table places it on a Google Generative AI endpoint family the shared contract does not model; it stays deliberately unresolved",
}];

/// The live-catalog models the current first-party table does not place.
///
/// A catalog entry proves a model exists; it does not say which endpoint
/// family serves it. These models stay unresolved.
const UNPLACED_CATALOG_MODELS: &[UnresolvedModel] = &[
    UnresolvedModel {
        model_id: "glm-5.1",
        endpoint_url: None,
        reason: "the 2026-10-22 table does not place it; a catalog entry does not say which endpoint family serves it",
    },
    UnresolvedModel {
        model_id: "minimax-m2.5",
        endpoint_url: None,
        reason: "the 2026-10-22 table does not place it, and it does not inherit minimax-m3's family",
    },
    UnresolvedModel {
        model_id: "omen-alpha",
        endpoint_url: None,
        reason: "the 2026-10-22 table does not place it; a catalog entry does not say which endpoint family serves it",
    },
    UnresolvedModel {
        model_id: "qwen3.6-plus",
        endpoint_url: None,
        reason: "the 2026-10-22 table does not place it; a catalog entry does not say which endpoint family serves it",
    },
    UnresolvedModel {
        model_id: "qwen3.7-max",
        endpoint_url: None,
        reason: "the 2026-10-22 table does not place it; a catalog entry does not say which endpoint family serves it",
    },
];

/// The committed catalog snapshot.
#[derive(Debug, serde::Deserialize)]
#[serde(deny_unknown_fields)]
struct CatalogSnapshot {
    snapshot_version: String,
    provider: String,
    source_url: String,
    retrieved_on: String,
    credential_used: bool,
    model_count: usize,
    models: Vec<String>,
}

/// The three committed artifacts the drift guard compares.
#[derive(Debug)]
struct ReviewedArtifacts {
    rows: Vec<ReviewedRow>,
    unresolved: Vec<UnresolvedModel>,
    hints: BTreeMap<String, WireSurface>,
    snapshot: BTreeSet<String>,
}

fn registry() -> ProviderProfileRegistry {
    ProviderProfileRegistry::embedded().expect("embedded provider profiles parse and validate")
}

fn opencode() -> eggpool_provider_profile::ProviderProfile {
    registry()
        .require("opencode-go")
        .expect("OpenCode Go is bundled")
        .clone()
}

/// The endpoint the current first-party table publishes for a surface family.
///
/// The Gemini families are absent because this provider does not serve them:
/// the table places its one Gemini model on a Google Generative AI endpoint
/// the shared contract does not model.
fn first_party_endpoint(surface: WireSurface) -> Option<&'static str> {
    match surface {
        WireSurface::OpenaiResponses => Some(RESPONSES_URL),
        WireSurface::OpenaiChatCompletions => Some(CHAT_URL),
        WireSurface::AnthropicMessages => Some(MESSAGES_URL),
        WireSurface::GeminiInteractions | WireSurface::GeminiGenerateContent => None,
    }
}

/// The path of the committed catalog snapshot.
fn catalog_snapshot_path() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR")).join(CATALOG_SNAPSHOT_PATH)
}

/// Parse a catalog snapshot document.
fn parse_catalog_snapshot(text: &str) -> Result<CatalogSnapshot, String> {
    serde_json::from_str(text).map_err(|error| format!("catalog snapshot is malformed: {error}"))
}

/// Read the committed catalog snapshot.
///
/// A missing or malformed snapshot is a hard failure of the guard: an absent
/// snapshot must never read as "the catalog is empty" and silently pass.
fn committed_catalog_snapshot() -> CatalogSnapshot {
    let path = catalog_snapshot_path();
    let text = fs::read_to_string(&path)
        .unwrap_or_else(|error| panic!("catalog snapshot {} is readable: {error}", path.display()));
    let snapshot = parse_catalog_snapshot(&text)
        .unwrap_or_else(|error| panic!("{error} at {}", path.display()));
    assert_eq!(snapshot.snapshot_version, "opencode-go-catalog.v1");
    assert_eq!(snapshot.provider, "opencode-go");
    assert_eq!(
        snapshot.source_url, "https://opencode.ai/zen/go/v1/models",
        "the snapshot records the public catalog it was read from"
    );
    assert!(
        !snapshot.credential_used,
        "the snapshot records that no credential was sent"
    );
    assert!(
        snapshot.retrieved_on.len() == 10
            && snapshot
                .retrieved_on
                .bytes()
                .all(|byte| byte.is_ascii_digit() || byte == b'-'),
        "the snapshot records its retrieval date as an ISO-8601 calendar date"
    );
    assert_eq!(
        snapshot.model_count,
        snapshot.models.len(),
        "the snapshot counts the ids it carries"
    );
    snapshot
}

/// The declared hints, keyed by model id.
fn declared_hints() -> BTreeMap<String, WireSurface> {
    opencode()
        .model_wire
        .iter()
        .map(|(model, preference)| (model.clone(), preference.preferred_surface))
        .collect()
}

/// The committed artifacts.
fn committed_artifacts() -> ReviewedArtifacts {
    let snapshot = committed_catalog_snapshot();
    ReviewedArtifacts {
        rows: REVIEWED_TABLE.to_vec(),
        unresolved: UNMODELLED_PLACEMENT
            .iter()
            .chain(UNPLACED_CATALOG_MODELS.iter())
            .copied()
            .collect(),
        hints: declared_hints(),
        snapshot: snapshot.models.into_iter().collect(),
    }
}

/// Compare the three committed artifacts and report every unexplained delta.
///
/// The guard fails closed. A model that is neither hinted nor recorded as
/// deliberately unresolved is a finding, never a warning: the guard is the
/// only mechanism that makes the reviewed record trustworthy.
fn drift_findings(artifacts: &ReviewedArtifacts) -> Vec<String> {
    let mut findings = Vec::new();

    let reviewed: BTreeSet<&str> = artifacts.rows.iter().map(|row| row.model_id).collect();
    let declared: BTreeSet<&str> = artifacts.hints.keys().map(String::as_str).collect();

    // The hints and the reviewed record are the same set.
    for model_id in declared.difference(&reviewed) {
        findings.push(format!("hint `{model_id}` has no reviewed record"));
    }
    for model_id in reviewed.difference(&declared) {
        findings.push(format!("reviewed model `{model_id}` has no hint"));
    }

    // A hint's surface agrees with the recorded placement.
    for row in &artifacts.rows {
        if let Some(hinted) = artifacts.hints.get(row.model_id)
            && *hinted != row.surface
        {
            findings.push(format!(
                "hint `{}` names `{}` but the record places it on `{}`",
                row.model_id,
                hinted.as_str(),
                row.surface.as_str()
            ));
        }
    }

    // Every catalog model is hinted or recorded as deliberately unresolved.
    let unresolved: BTreeSet<&str> = artifacts
        .unresolved
        .iter()
        .map(|entry| entry.model_id)
        .collect();
    for model_id in &artifacts.snapshot {
        if !declared.contains(model_id.as_str()) && !unresolved.contains(model_id.as_str()) {
            findings.push(format!(
                "catalog model `{model_id}` has no hint and no recorded unresolved entry"
            ));
        }
    }

    // Every hinted model the catalog dropped is recorded as withdrawn, and
    // every recorded withdrawal is one the catalog really dropped.
    for row in &artifacts.rows {
        let listed = artifacts.snapshot.contains(row.model_id);
        match row.catalog_state {
            CatalogState::Listed if !listed => findings.push(format!(
                "reviewed model `{}` is absent from the catalog snapshot but is not recorded as withdrawn",
                row.model_id
            )),
            CatalogState::Withdrawn if listed => findings.push(format!(
                "reviewed model `{}` is recorded as withdrawn but the catalog still lists it",
                row.model_id
            )),
            _ => {}
        }
    }

    findings
}

#[test]
fn base_url_and_per_surface_paths_compose_the_profile_dispatch_urls() {
    let profile = opencode();
    assert_eq!(profile.base_url, "https://opencode.ai/zen/go/v1");
    assert_eq!(
        profile
            .surface_url(WireSurface::OpenaiChatCompletions)
            .as_deref(),
        Some(PROFILE_CHAT_URL)
    );
    assert_eq!(
        profile.surface_url(WireSurface::OpenaiResponses).as_deref(),
        Some(PROFILE_RESPONSES_URL)
    );
    assert_eq!(
        profile
            .surface_url(WireSurface::AnthropicMessages)
            .as_deref(),
        Some(PROFILE_MESSAGES_URL)
    );
}

#[test]
fn reviewed_endpoints_are_the_ones_the_current_first_party_table_publishes() {
    // Each row's endpoint URL must be the one the current table publishes for
    // the surface family it places the model on.
    for row in REVIEWED_TABLE {
        match row.provenance {
            Provenance::CurrentTable => assert_eq!(
                row.endpoint_url,
                first_party_endpoint(row.surface),
                "{} publishes the endpoint the current table publishes for it",
                row.model_id
            ),
            Provenance::CarriedForward => assert_eq!(
                row.endpoint_url, None,
                "{} is no longer placed, so the current table publishes no endpoint for it",
                row.model_id
            ),
        }
    }
    for entry in UNMODELLED_PLACEMENT {
        assert_eq!(
            entry.endpoint_url,
            Some(
                "https://opencode.ai/inference/go/google/v1beta/models/gemini-4-argon-contributor"
            ),
            "{} is published on the Google Generative AI endpoint",
            entry.model_id
        );
    }
}

#[test]
fn per_surface_authentication_matches_first_party_documentation() {
    let profile = opencode();
    // The selected surface supplies the endpoint-specific auth shape.
    for surface in [
        WireSurface::OpenaiChatCompletions,
        WireSurface::OpenaiResponses,
    ] {
        let auth = profile
            .surface_auth(surface)
            .expect("surface declares an auth shape");
        assert_eq!(auth.mode, ProviderAuthMode::Bearer, "{surface:?}");
        assert_eq!(auth.header, "Authorization", "{surface:?}");
    }
    let messages = profile
        .surface_auth(WireSurface::AnthropicMessages)
        .expect("messages surface declares an auth shape");
    assert_eq!(messages.mode, ProviderAuthMode::ApiKey);
    assert_eq!(messages.header, "x-api-key");
}

#[test]
fn model_discovery_composes_under_the_provider_base() {
    let profile = opencode();
    let endpoint = profile.resolved_models_endpoint();
    assert_eq!(endpoint.method, "GET");
    assert!(endpoint.required, "catalog discovery is required");
    assert_eq!(
        eggpool_provider_profile::compose_url(&profile.base_url, &endpoint.path),
        "https://opencode.ai/zen/go/v1/models"
    );
}

#[test]
fn reviewed_table_is_covered_exhaustively_by_non_fixed_hints() {
    let profile = opencode();
    for row in REVIEWED_TABLE {
        let preference = profile
            .model_wire_preference(row.model_id)
            .unwrap_or_else(|| panic!("{} has a reviewed hint", row.model_id));
        assert_eq!(
            preference.preferred_surface, row.surface,
            "{} surface",
            row.model_id
        );
        assert!(!preference.fixed, "{} stays advisory", row.model_id);
    }

    // Exhaustive in the other direction too: the registry declares no hint
    // beyond the reviewed table.
    let reviewed: BTreeSet<&str> = REVIEWED_TABLE.iter().map(|row| row.model_id).collect();
    let declared: BTreeSet<&str> = profile.model_wire.keys().map(String::as_str).collect();
    assert_eq!(
        declared, reviewed,
        "hints and the reviewed first-party table stay identical"
    );
    assert_eq!(declared.len(), REVIEWED_TABLE.len());
}

#[test]
fn every_hint_is_advisory_and_names_a_surface_the_provider_serves() {
    let profile = opencode();
    for (model_id, preference) in &profile.model_wire {
        assert!(!preference.fixed, "{model_id} stays advisory");
        assert!(
            profile
                .wire_surfaces
                .contains_key(&preference.preferred_surface),
            "{model_id} names a surface the provider serves"
        );
    }
}

#[test]
fn the_models_the_current_table_places_now_resolve() {
    // The four placements the 2026-10-07 review did not document. Each comes
    // from the current first-party table, not from a family-name inference.
    let profile = opencode();
    for (model_id, surface) in [
        ("beam-free", WireSurface::OpenaiChatCompletions),
        (
            "minimax-m3.1-flash-free",
            WireSurface::OpenaiChatCompletions,
        ),
        ("step-5-preview", WireSurface::OpenaiChatCompletions),
        ("claude-haiku-5-5", WireSurface::AnthropicMessages),
    ] {
        let preference = profile
            .model_wire_preference(model_id)
            .unwrap_or_else(|| panic!("{model_id} resolves"));
        assert_eq!(preference.preferred_surface, surface, "{model_id}");
        assert!(!preference.fixed, "{model_id} stays advisory");
    }
}

#[test]
fn every_reviewed_row_records_its_disposition_and_reason() {
    for row in REVIEWED_TABLE {
        assert!(
            !row.reason.trim().is_empty(),
            "{} records a reason for its disposition",
            row.model_id
        );
    }

    // The six entries the current table no longer places each carry the
    // carried-forward rationale, and none was dropped.
    let carried_forward: BTreeSet<&str> = REVIEWED_TABLE
        .iter()
        .filter(|row| row.provenance == Provenance::CarriedForward)
        .map(|row| row.model_id)
        .collect();
    assert_eq!(
        carried_forward,
        BTreeSet::from([
            "deepseek-v4-flash",
            "deepseek-v4-flash-vision-exp",
            "grok-4.6",
            "kimi-k2.6",
            "minimax-m2.7",
            "space-bunny",
        ]),
        "the six entries the current table no longer places are recorded, not dropped"
    );
    for row in REVIEWED_TABLE
        .iter()
        .filter(|row| row.provenance == Provenance::CarriedForward)
    {
        assert!(
            row.reason.contains("2026-10-22") && row.reason.contains("2026-10-07"),
            "{} records why its placement is carried forward rather than retracted",
            row.model_id
        );
    }

    // The four placements the 2026-10-07 review did not document are the ones
    // this review added.
    let newly_placed: BTreeSet<&str> = REVIEWED_TABLE
        .iter()
        .filter(|row| row.reason == PLACED_BY_CURRENT_TABLE_NEW)
        .map(|row| row.model_id)
        .collect();
    assert_eq!(
        newly_placed,
        BTreeSet::from([
            "beam-free",
            "claude-haiku-5-5",
            "minimax-m3.1-flash-free",
            "step-5-preview",
        ]),
        "the four placements the 2026-10-07 review did not document"
    );

    // A row the current table still places keeps a published endpoint; a
    // carried-forward row has none, because the table publishes none for it.
    for row in REVIEWED_TABLE {
        match row.provenance {
            Provenance::CurrentTable => assert!(
                row.endpoint_url.is_some(),
                "{} is still placed, so the table publishes an endpoint for it",
                row.model_id
            ),
            Provenance::CarriedForward => assert!(
                row.endpoint_url.is_none(),
                "{} is no longer placed, so the table publishes no endpoint for it",
                row.model_id
            ),
        }
    }
}

#[test]
fn the_deliberately_unresolved_models_stay_unresolved() {
    let registry = registry();
    for entry in UNMODELLED_PLACEMENT
        .iter()
        .chain(UNPLACED_CATALOG_MODELS.iter())
    {
        assert!(
            registry
                .model_wire_preference("opencode-go", entry.model_id)
                .is_none(),
            "{} stays unresolved: {}",
            entry.model_id,
            entry.reason
        );
    }
}

#[test]
fn representative_lookups_return_the_documented_surface() {
    let registry = registry();
    for (model_id, surface) in [
        ("gpt-6-luna", WireSurface::OpenaiResponses),
        ("gpt-5.6-luna", WireSurface::OpenaiResponses),
        ("grok-4.7", WireSurface::OpenaiResponses),
        ("grok-4.6", WireSurface::OpenaiResponses),
        ("muse-spark-1.3-contributor", WireSurface::OpenaiResponses),
        ("glm-5.3-flash", WireSurface::OpenaiChatCompletions),
        ("kimi-k3", WireSurface::OpenaiChatCompletions),
        ("deepseek-v4-pro", WireSurface::OpenaiChatCompletions),
        ("mimo-v2.6-flash", WireSurface::OpenaiChatCompletions),
        ("hy4-preview", WireSurface::OpenaiChatCompletions),
        ("longcat-2.0", WireSurface::OpenaiChatCompletions),
        ("space-bunny", WireSurface::OpenaiChatCompletions),
        ("minimax-m3", WireSurface::AnthropicMessages),
        ("qwen3.8-max", WireSurface::AnthropicMessages),
    ] {
        let preference = registry
            .model_wire_preference("opencode-go", model_id)
            .unwrap_or_else(|| panic!("{model_id} resolves"));
        assert_eq!(preference.preferred_surface, surface, "{model_id}");
    }
}

#[test]
fn unknown_models_and_unknown_providers_stay_unresolved() {
    let registry = registry();
    for unknown in [
        // Model ids that appear in a public catalog or a sibling product but
        // are absent from the reviewed first-party endpoint table.
        "kimi-k3-turbo",
        "gpt-5.5-mini",
        "glm-5",
        "",
    ] {
        assert!(
            registry
                .model_wire_preference("opencode-go", unknown)
                .is_none(),
            "{unknown:?} has no fabricated hint"
        );
    }
    // A provider with no reviewed hints resolves nothing.
    assert!(
        registry
            .model_wire_preference("together", "llama-3.3-70b")
            .is_none()
    );
    assert!(
        registry
            .model_wire_preference("not-a-provider", "gpt-6-luna")
            .is_none()
    );
}

#[test]
fn ambiguous_support_without_a_hint_is_not_defaulted_to_chat_completions() {
    // OpenCode Go serves three surfaces; an unresolved model must not be
    // silently treated as Chat Completions.
    let profile = opencode();
    assert!(
        profile
            .surface_path_template(WireSurface::OpenaiChatCompletions)
            .is_some()
    );
    assert!(
        profile.model_wire_preference("unreviewed-model").is_none(),
        "an ambiguous model stays unresolved rather than defaulting"
    );
}

#[test]
fn opencode_go_is_not_classified_as_credential_proven() {
    use eggpool_provider_profile::{CatalogEvidence, CredentialProof, CredentialStatus};

    let profile = opencode();
    // The profile records that its catalog needs no credential.
    assert_eq!(profile.verify.models_require_authentication, Some(false));
    assert_eq!(
        profile.verify.catalog_authentication(),
        eggpool_provider_profile::CatalogAuthentication::Public
    );

    let policy = profile.verification_policy();
    assert_eq!(
        policy.credential_proof,
        CredentialProof::DeferredUntilInference
    );
    assert_eq!(
        policy.assess(CatalogEvidence::PublicStatic),
        CredentialStatus::Unverified
    );
    assert!(!policy.catalog_implies_credential(CatalogEvidence::PublicStatic));
}

#[test]
fn catalog_drift_guard_passes_on_the_committed_artifacts() {
    let artifacts = committed_artifacts();
    assert_eq!(
        drift_findings(&artifacts),
        Vec::<String>::new(),
        "the wire map, the reviewed record, and the catalog snapshot agree"
    );
}

#[test]
fn catalog_drift_guard_fails_on_an_injected_unexplained_catalog_id() {
    let mut artifacts = committed_artifacts();
    // A catalog model with no hint and no recorded disposition.
    artifacts.snapshot.insert("injected-drift-model".to_owned());
    let findings = drift_findings(&artifacts);
    assert!(
        findings
            .iter()
            .any(|finding| finding.contains("injected-drift-model")),
        "the guard reports the unexplained catalog id: {findings:?}"
    );
}

#[test]
fn catalog_drift_guard_fails_on_an_injected_surface_disagreement() {
    let mut artifacts = committed_artifacts();
    artifacts
        .hints
        .insert("beam-free".to_owned(), WireSurface::AnthropicMessages);
    let findings = drift_findings(&artifacts);
    assert!(
        findings.iter().any(|finding| finding.contains("beam-free")),
        "the guard reports the surface disagreement: {findings:?}"
    );
}

#[test]
fn catalog_drift_guard_fails_on_an_injected_missing_withdrawn_record() {
    let mut artifacts = committed_artifacts();
    // A hinted model the catalog no longer lists, without a recorded
    // withdrawal.
    artifacts.snapshot.remove("beam-free");
    let findings = drift_findings(&artifacts);
    assert!(
        findings.iter().any(|finding| finding.contains("beam-free")),
        "the guard reports the unrecorded withdrawal: {findings:?}"
    );

    // The other direction: a recorded withdrawal the catalog has reversed.
    let mut artifacts = committed_artifacts();
    artifacts
        .snapshot
        .insert("longcat-2.5-preview-free".to_owned());
    let findings = drift_findings(&artifacts);
    assert!(
        findings
            .iter()
            .any(|finding| finding.contains("longcat-2.5-preview-free")),
        "the guard reports the reversed withdrawal: {findings:?}"
    );
}

#[test]
fn catalog_drift_guard_fails_on_a_missing_or_malformed_snapshot() {
    // An absent or malformed snapshot is a hard failure, never an empty set.
    assert!(
        parse_catalog_snapshot("not json").is_err(),
        "a malformed snapshot is rejected"
    );
    assert!(
        parse_catalog_snapshot("{}").is_err(),
        "a snapshot without the required fields is rejected"
    );
    assert!(
        parse_catalog_snapshot(r#"{"snapshot_version":"opencode-go-catalog.v1"}"#).is_err(),
        "a partial snapshot is rejected"
    );
    assert!(
        catalog_snapshot_path().is_file(),
        "the catalog snapshot is committed beside the fixture"
    );
}
