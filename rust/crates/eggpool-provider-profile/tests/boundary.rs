//! Static boundary guards for the shared crate.
//!
//! These tests are the mechanical form of the crate's security and ownership
//! invariants: no runtime-only dependency, no secret-bearing data, and exactly
//! one canonical provider-profile asset in the repository.

use std::fs;
use std::path::{Path, PathBuf};

/// Repository root, derived from this crate's manifest directory.
fn repository_root() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .ancestors()
        .nth(3)
        .expect("crate lives under rust/crates/<name>")
        .to_path_buf()
}

/// Every `.rs` file in the crate's own source tree.
fn crate_sources() -> Vec<PathBuf> {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("src");
    let mut sources = Vec::new();
    let mut stack = vec![root];
    while let Some(directory) = stack.pop() {
        for entry in fs::read_dir(&directory).expect("crate source directory is readable") {
            let path = entry.expect("directory entry is readable").path();
            if path.is_dir() {
                stack.push(path);
            } else if path.extension().is_some_and(|extension| extension == "rs") {
                sources.push(path);
            }
        }
    }
    assert!(!sources.is_empty(), "crate sources are present");
    sources
}

#[test]
fn crate_has_no_runtime_only_dependency() {
    let manifest = fs::read_to_string(Path::new(env!("CARGO_MANIFEST_DIR")).join("Cargo.toml"))
        .expect("crate manifest is readable");
    let dependencies = manifest
        .split("[dependencies]")
        .nth(1)
        .expect("crate declares a dependency table")
        .split("\n[")
        .next()
        .expect("dependency table is terminated");

    let declared: Vec<&str> = dependencies
        .lines()
        .filter(|line| {
            let trimmed = line.trim();
            !trimmed.is_empty() && !trimmed.starts_with('#') && trimmed.contains('=')
        })
        .filter_map(|line| line.split('=').next())
        .map(str::trim)
        .collect();
    assert_eq!(
        declared,
        vec!["eggpool-wire", "serde", "thiserror", "toml"],
        "the shared crate depends only on the neutral wire surface vocabulary and parsers"
    );

    for forbidden in [
        "tokio",
        "axum",
        "hyper",
        "reqwest",
        "eggfetch",
        "eggress",
        "rusqlite",
        "sqlx",
        "rusqlite",
        "std::env",
        "std::net",
        "std::fs",
        "std::process",
        "std::time",
        "log",
        "tracing",
        "rand",
    ] {
        assert!(
            !dependencies.contains(forbidden),
            "the shared crate declares no `{forbidden}` dependency"
        );
    }
}

#[test]
fn crate_sources_import_no_runtime_or_eggpool_module() {
    for source in crate_sources() {
        let text = fs::read_to_string(&source).expect("crate source is readable");
        for forbidden in [
            "std::env",
            "std::net",
            "std::fs",
            "std::process",
            "std::time",
            "tokio",
            "axum",
            "hyper",
            "rusqlite",
            "reqwest",
            "eggpool::",
            "std::thread",
        ] {
            assert!(
                !text.contains(forbidden),
                "{} references forbidden runtime module {forbidden}",
                source.display()
            );
        }
    }
}

#[test]
fn exactly_one_canonical_provider_profile_asset_exists() {
    let root = repository_root();
    let mut assets = Vec::new();
    let mut stack = vec![root];
    while let Some(directory) = stack.pop() {
        for entry in fs::read_dir(&directory).expect("repository directory is readable") {
            let path = entry.expect("directory entry is readable").path();
            let name = path
                .file_name()
                .map(|name| name.to_string_lossy().into_owned())
                .unwrap_or_default();
            if name.starts_with('.')
                || name == "target"
                || name == "artifacts"
                || name == "node_modules"
            {
                if path.is_dir() {
                    continue;
                }
                continue;
            }
            if path.is_dir() {
                stack.push(path);
                continue;
            }
            // Any file carrying a canonical bundled provider-profile name
            // would be a second live copy of these facts.
            if matches!(
                name.as_str(),
                "_provider_profiles.toml"
                    | "provider_profiles.toml"
                    | "providers.toml"
                    | "_providers.toml"
                    | "_templates.toml"
            ) {
                assets.push(path);
            }
        }
    }
    assets.sort();
    assert_eq!(
        assets,
        vec![
            repository_root()
                .join("rust")
                .join("crates")
                .join("eggpool-provider-profile")
                .join("assets")
                .join("_provider_profiles.toml")
        ],
        "provider-profile metadata has exactly one canonical, editable asset"
    );
}

#[test]
fn eggpool_runtime_and_consumer_fixture_are_the_only_profile_consumers() {
    // The owning runtime and the sibling-consumer fixture read the canonical
    // data through this crate; nothing else carries a bundled profile copy.
    let root = repository_root();
    for relative in [
        "rust/src/operations/config_mutation.rs",
        "rust/crates/eggpool-provider-profile/consumer-fixture/src/lib.rs",
    ] {
        assert!(
            root.join(relative).exists(),
            "{relative} consumes the shared profile contract"
        );
    }
    assert!(
        !root.join("rust/assets/providers/_templates.toml").exists(),
        "the historical provider-template asset path no longer exists"
    );
}
