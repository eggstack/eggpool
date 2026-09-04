use std::{collections::BTreeMap, env, fs, path::PathBuf, process};

fn main() {
    let manifest_dir = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap());
    let schema_dir = manifest_dir.join("../src/eggpool/db/schema");
    let checksum_path = schema_dir.join("checksums.json");
    println!("cargo:rerun-if-changed={}", checksum_path.display());

    let checksums = parse_checksums(
        &fs::read_to_string(&checksum_path)
            .unwrap_or_else(|error| panic!("cannot read canonical migration checksums: {error}")),
    );
    let mut migrations = Vec::new();
    let mut versions = BTreeMap::new();

    let mut paths: Vec<_> = fs::read_dir(&schema_dir)
        .unwrap_or_else(|error| panic!("cannot read canonical schema directory: {error}"))
        .filter_map(|entry| entry.ok().map(|entry| entry.path()))
        .filter(|path| path.extension().is_some_and(|extension| extension == "sql"))
        .collect();
    paths.sort();

    for path in paths {
        let name = path.file_name().unwrap().to_string_lossy().into_owned();
        let version = name
            .split_once('_')
            .and_then(|(prefix, _)| prefix.parse::<u32>().ok())
            .unwrap_or_else(|| panic!("canonical migration has invalid name: {name}"));
        if versions.insert(version, name.clone()).is_some() {
            panic!("canonical migrations contain duplicate version {version}");
        }
        let expected = checksums
            .get(&name)
            .unwrap_or_else(|| panic!("canonical migration is absent from checksums.json: {name}"));
        println!("cargo:rerun-if-changed={}", path.display());
        migrations.push((version, name, path, expected.clone()));
    }

    if migrations.len() != checksums.len() {
        panic!(
            "canonical migration/checksum inventory differs: {} SQL files, {} checksums",
            migrations.len(),
            checksums.len()
        );
    }

    let out_dir = PathBuf::from(env::var_os("OUT_DIR").unwrap());
    let generated_path = out_dir.join("eggpool_migrations.rs");
    let mut generated = String::from("pub(crate) static MIGRATIONS: &[Migration] = &[\n");
    for (version, name, path, checksum) in migrations {
        let include_path = path.to_str().unwrap();
        generated.push_str(&format!(
            "    Migration {{ version: {version}, name: {name:?}, sql: include_str!({include_path:?}), expected_sha256: {checksum:?} }},\n"
        ));
    }
    generated.push_str("];");
    fs::write(generated_path, generated).unwrap_or_else(|error| {
        eprintln!("cannot write generated migration inventory: {error}");
        process::exit(1);
    });
}

fn parse_checksums(manifest: &str) -> BTreeMap<String, String> {
    let mut checksums = BTreeMap::new();
    for line in manifest.lines() {
        let pieces: Vec<_> = line.split('"').collect();
        if pieces.len() < 4 || !pieces[1].ends_with(".sql") {
            continue;
        }
        let name = pieces[1].to_owned();
        let checksum = pieces[3].to_owned();
        if checksum.len() != 64 || !checksum.bytes().all(|byte| byte.is_ascii_hexdigit()) {
            panic!("invalid SHA-256 checksum for canonical migration: {name}");
        }
        if checksums.insert(name.clone(), checksum).is_some() {
            panic!("duplicate canonical migration checksum: {name}");
        }
    }
    if checksums.is_empty() {
        panic!("canonical migration checksum manifest is empty or malformed");
    }
    checksums
}
