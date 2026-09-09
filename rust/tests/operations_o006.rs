//! O006 data-safety contracts: migrations, VACUUM, archive validation, and
//! the process-owned automatic-backup callback registration.

use std::{fs, io::Write, path::Path};

use eggpool::{
    Config,
    db::{Database, DatabaseConfig, MigrationRunner},
    operations::backup::{BackupError, BackupService},
    runtime_lifecycle::ProcessRuntime,
};
use tempfile::TempDir;

async fn migrated_database(path: &Path) -> Database {
    let database = Database::open(DatabaseConfig {
        path: path.display().to_string(),
        ..Default::default()
    })
    .await
    .expect("database opens");
    MigrationRunner::new(&database)
        .run()
        .await
        .expect("canonical migrations apply");
    database
}

fn project(directory: &TempDir) -> (Config, std::path::PathBuf) {
    let config_path = directory.path().join("config.toml");
    let db_path = directory.path().join("usage.sqlite3");
    let mut config = Config::default();
    config.database.path = db_path.display().to_string();
    config.backup.enabled = true;
    config.backup.interval_s = 60;
    fs::write(&config_path, toml::to_string(&config).expect("config TOML")).expect("config");
    (config, config_path)
}

#[tokio::test]
async fn backup_uses_sqlite_snapshot_and_collision_safe_stored_archive() {
    let directory = tempfile::tempdir().expect("temp root");
    let (config, config_path) = project(&directory);
    let database = migrated_database(&directory.path().join("usage.sqlite3")).await;
    let service = BackupService::from_config(&config_path, &config)
        .with_output_dir(directory.path().join("backups"));
    assert_eq!(
        service.paths.database,
        directory.path().join("usage.sqlite3")
    );
    assert_eq!(service.paths.config, config_path);

    let first = service.create(&database).await.expect("first backup");
    let second = service.create(&database).await.expect("second backup");
    assert_ne!(first.archive, second.archive);
    assert!(
        first
            .archive
            .file_name()
            .unwrap()
            .to_string_lossy()
            .contains(".zip")
    );
    assert_eq!(first.members, vec!["config.toml", "usage.sqlite3"]);

    let archive = fs::File::open(&first.archive).expect("archive");
    let mut zip = zip::ZipArchive::new(archive).expect("valid zip");
    assert_eq!(
        zip.by_name("META").expect("META").compression(),
        zip::CompressionMethod::Stored
    );
    assert!(
        zip.by_name("usage.sqlite3")
            .expect("database member")
            .size()
            > 0
    );
    database.close().await.expect("database close");
}

#[tokio::test]
async fn recover_validates_and_atomically_restores_config_and_database() {
    let directory = tempfile::tempdir().expect("temp root");
    let (config, config_path) = project(&directory);
    let db_path = directory.path().join("usage.sqlite3");
    let database = migrated_database(&db_path).await;
    let service = BackupService::from_config(&config_path, &config)
        .with_output_dir(directory.path().join("backups"));
    let archive = service.create(&database).await.expect("backup").archive;
    database.close().await.expect("database close");

    let expected_config = fs::read(&config_path).expect("original config");
    fs::write(&config_path, b"[server\n").expect("corrupt current config");
    let restored = service.recover(&archive).expect("restore");
    assert_eq!(restored.config, config_path);
    assert_eq!(
        fs::read(&config_path).expect("restored config"),
        expected_config
    );
}

#[test]
fn traversal_archive_is_rejected_before_target_access() {
    let directory = tempfile::tempdir().expect("temp root");
    let archive_path = directory.path().join("bad.zip");
    let file = fs::File::create(&archive_path).expect("archive");
    let mut zip = zip::ZipWriter::new(file);
    let options = zip::write::SimpleFileOptions::default()
        .compression_method(zip::CompressionMethod::Stored)
        .unix_permissions(0o600);
    zip.start_file("META", options).expect("META");
    zip.write_all(b"format_version = 1\nconfig_path = \"/tmp/config.toml\"\ndb_path = \"/tmp/usage.sqlite3\"\nmembers = [\"../config.toml\", \"usage.sqlite3\"]\n").expect("META bytes");
    zip.start_file("../config.toml", options)
        .expect("traversal");
    zip.write_all(b"bad").expect("config bytes");
    zip.start_file("usage.sqlite3", options).expect("db");
    zip.write_all(b"not sqlite").expect("db bytes");
    zip.finish().expect("finish");

    let service = BackupService {
        paths: eggpool::operations::backup::BackupPaths {
            config: directory.path().join("config.toml"),
            database: directory.path().join("usage.sqlite3"),
            env: None,
            output_dir: directory.path().to_owned(),
        },
        include_env: false,
        install_method: "rust".to_owned(),
    };
    assert!(matches!(
        service.recover(&archive_path),
        Err(BackupError::InvalidArchive(_))
    ));
}

#[tokio::test]
async fn vacuum_and_automatic_backup_use_existing_database_and_supervisor_boundaries() {
    let directory = tempfile::tempdir().expect("temp root");
    let (config, config_path) = project(&directory);
    let database = migrated_database(&directory.path().join("usage.sqlite3")).await;
    database.vacuum().await.expect("vacuum");
    let process =
        ProcessRuntime::with_config_path_and_config(database.clone(), &config_path, &config)
            .expect("process runtime");
    let backup = process
        .task_capability_inventory()
        .into_iter()
        .find(|capability| capability.name == "automatic_backup")
        .expect("automatic backup inventory");
    assert!(backup.registered);
    assert_eq!(backup.future_owner, None);
    database.close().await.expect("database close");
}
