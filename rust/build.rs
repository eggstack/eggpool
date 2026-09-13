use std::{collections::BTreeMap, env, fs, path::PathBuf, process};

mod build_support;

fn main() {
    let manifest_dir = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap());
    let repository_dir = manifest_dir
        .parent()
        .unwrap_or_else(|| panic!("Cargo manifest is not inside the repository"));
    let config_paths = ["config.example.toml", "config.sbc.example.toml"];
    for relative_path in config_paths {
        let path = repository_dir.join(relative_path);
        println!("cargo:rerun-if-changed={}", path.display());
        fs::read_to_string(&path).unwrap_or_else(|error| {
            panic!(
                "cannot read canonical configuration example {}: {error}",
                path.display()
            )
        });
    }

    let schema_dir = manifest_dir.join("assets/db/migrations");
    let checksum_path = schema_dir.join("checksums.json");
    println!("cargo:rerun-if-changed={}", checksum_path.display());

    let checksums = build_support::parse_checksums(
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

    let default_config_path = repository_dir.join("config.example.toml");
    let config_generated = format!(
        "pub(crate) const DEFAULT_CONFIG: &str = include_str!({:?});\n",
        default_config_path.to_string_lossy()
    );
    fs::write(out_dir.join("eggpool_config_assets.rs"), config_generated).unwrap_or_else(|error| {
        eprintln!("cannot write generated configuration asset: {error}");
        process::exit(1);
    });
}
