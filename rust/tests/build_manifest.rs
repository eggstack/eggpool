use std::{collections::BTreeSet, fs, path::PathBuf};

use serde::Deserialize;
use sha2::{Digest, Sha256};

#[path = "../build_support.rs"]
mod build_support;

#[derive(Debug, Deserialize)]
struct RuntimeManifest {
    manifest_version: String,
    assets: Vec<RuntimeAsset>,
    schema_migration_count: usize,
    schema_change: bool,
}

#[derive(Debug, Deserialize)]
struct RuntimeAsset {
    path: String,
    category: String,
    sha256: String,
}

#[test]
fn semantically_equivalent_reformatted_manifest_is_accepted() {
    let original = include_str!("../assets/db/migrations/checksums.json");
    let value: serde_json::Value = serde_json::from_str(original).expect("canonical JSON");
    let reformatted = serde_json::to_string(&value).expect("compact JSON");

    let parsed = build_support::parse_checksums(&reformatted);
    assert_eq!(parsed.len(), 54);
    assert_eq!(
        parsed.get("0001_initial.sql").map(String::as_str),
        Some("2aa5800147fc3ef8cc35591c44564244dba138b4dbc92fc1779e4f88813912ad")
    );
}

#[test]
fn malformed_checksum_content_is_rejected() {
    for manifest in [
        r#"{"files":{"0001_initial.sql":1}}"#,
        r#"{"files":{"0001_initial.sql":"not-a-sha"}}"#,
        r#"{"files":{"0001_initial.sql":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","0001_initial.sql":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}}"#,
    ] {
        assert!(
            std::panic::catch_unwind(|| build_support::parse_checksums(manifest)).is_err(),
            "manifest should be rejected: {manifest}"
        );
    }
}

#[test]
fn rust_owned_runtime_assets_are_complete_and_hash_locked() {
    let manifest: RuntimeManifest =
        serde_json::from_str(include_str!("../assets/runtime-manifest.json"))
            .expect("runtime asset manifest is valid JSON");
    assert_eq!(manifest.manifest_version, "runtime-assets.v1");
    assert_eq!(manifest.schema_migration_count, 54);
    assert!(!manifest.schema_change);

    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let mut categories = BTreeSet::new();
    for asset in manifest.assets {
        categories.insert(asset.category);
        let path = root.join("assets").join(&asset.path);
        let bytes = fs::read(&path).expect("Rust-owned runtime asset exists");
        let digest = Sha256::digest(&bytes);
        let actual = digest
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        assert_eq!(actual, asset.sha256, "runtime asset drift: {}", asset.path);
    }
    assert_eq!(
        categories,
        BTreeSet::from([
            "release-catalog".to_owned(),
            "configuration-template".to_owned(),
            "dashboard-assets".to_owned(),
            "provider-templates".to_owned(),
            "sqlite-migrations".to_owned(),
            "wire-profiles".to_owned(),
        ])
    );
}

#[test]
fn historical_python_application_tree_is_absent() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    assert!(!root.join("../src/eggpool").exists());
}
