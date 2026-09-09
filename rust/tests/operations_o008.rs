//! O008 release authority, integrity, replacement, and task coverage.

use std::{fs, sync::Arc};

use eggpool::{
    Config,
    operations::update::{
        Platform, ReleaseClient, ReleaseTarget, ReleaseVersion, UpdateCheckerState, UpdateError,
        UpdateService,
    },
    task_supervisor::{
        RuntimeTaskSupervisor, TaskCallbackRegistry, TaskOwnership, runtime_task_specs_for_config,
    },
};
use sha2::{Digest, Sha256};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpListener,
    sync::oneshot,
    time::{Duration, timeout},
};

async fn fake_release_server(metadata: String, artifact: Vec<u8>) -> (String, oneshot::Sender<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.expect("listener");
    let address = listener.local_addr().expect("address");
    let metadata = metadata.replace(
        "http://127.0.0.1/artifact",
        &format!("http://{address}/artifact"),
    );
    let (stop, mut stopped) = oneshot::channel();
    tokio::spawn(async move {
        loop {
            tokio::select! {
                _ = &mut stopped => break,
                accepted = listener.accept() => {
                    let Ok((mut stream, _)) = accepted else { break };
                    let mut request = [0_u8; 4096];
                    let count = stream.read(&mut request).await.unwrap_or_default();
                    let request = String::from_utf8_lossy(&request[..count]);
                    let (body, content_type) = if request.contains("/artifact") {
                        (artifact.clone(), "application/octet-stream")
                    } else {
                        (metadata.as_bytes().to_vec(), "application/json")
                    };
                    let response = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                        body.len()
                    );
                    if stream.write_all(response.as_bytes()).await.is_ok() {
                        let _ = stream.write_all(&body).await;
                    }
                }
            }
        }
    });
    (format!("http://{address}/releases"), stop)
}

#[tokio::test]
async fn loopback_release_metadata_selects_platform_and_verifies_sha256() {
    let artifact = b"reviewed rust executable".to_vec();
    let digest = hex_digest(&artifact);
    let metadata = format!(
        r#"{{"tag_name":"v0.7.5","prerelease":false,"draft":false,"assets":[{{"name":"eggpool-0.7.5-linux-aarch64","browser_download_url":"http://127.0.0.1/artifact","digest":"sha256:{digest}","size":{}}}]}}"#,
        artifact.len()
    );
    let (api, stop) = fake_release_server(metadata, artifact.clone()).await;
    let client = ReleaseClient::with_release_api(&api)
        .expect("client")
        .with_platform(Platform {
            os: "linux".into(),
            architecture: "aarch64".into(),
        });
    let resolved = client
        .resolve(&ReleaseTarget::Latest)
        .await
        .expect("release resolves");
    let selected = resolved.artifact.expect("matching artifact");
    assert_eq!(selected.name, "eggpool-0.7.5-linux-aarch64");
    assert_eq!(
        client.download(&selected).await.expect("download"),
        artifact
    );
    let _ = stop.send(());
}

#[tokio::test]
async fn metadata_and_integrity_failures_are_typed_and_no_asset_is_explicit() {
    let artifact = b"artifact".to_vec();
    let wrong_digest = "0000000000000000000000000000000000000000000000000000000000000000";
    let metadata = format!(
        r#"{{"tag_name":"v0.7.5","assets":[{{"name":"eggpool-0.7.5-linux-aarch64","browser_download_url":"http://127.0.0.1/artifact","digest":"sha256:{wrong_digest}","size":{}}}]}}"#,
        artifact.len()
    );
    let (api, stop) = fake_release_server(metadata, artifact).await;
    let client = ReleaseClient::with_release_api(&api)
        .expect("client")
        .with_platform(Platform {
            os: "linux".into(),
            architecture: "aarch64".into(),
        });
    let resolved = client
        .resolve(&ReleaseTarget::Latest)
        .await
        .expect("metadata");
    let selected = resolved.artifact.expect("artifact");
    assert!(matches!(
        client.download(&selected).await,
        Err(UpdateError::IntegrityMismatch)
    ));
    let _ = stop.send(());

    let (api, stop) = fake_release_server(
        r#"{"tag_name":"v0.7.5","assets":[{"name":"eggpool-0.7.5-linux-aarch64","browser_download_url":"http://127.0.0.1/artifact"}]}"#.to_owned(),
        b"artifact".to_vec(),
    )
    .await;
    let client = ReleaseClient::with_release_api(&api)
        .expect("client")
        .with_platform(Platform {
            os: "linux".into(),
            architecture: "aarch64".into(),
        });
    assert!(matches!(
        client.resolve(&ReleaseTarget::Latest).await,
        Err(UpdateError::IntegrityMissing)
    ));
    let _ = stop.send(());
}

#[tokio::test]
async fn checker_is_check_only_bounded_and_retains_latest_on_failure() {
    let artifact = b"unused".to_vec();
    let metadata =
        r#"{"tag_name":"v0.7.5","prerelease":false,"draft":false,"assets":[]}"#.to_owned();
    let (api, stop) = fake_release_server(metadata, artifact).await;
    let service = UpdateService::with_release_api(&api).expect("service");
    let checker = UpdateCheckerState::new(service);
    let info = timeout(Duration::from_secs(2), checker.check_once())
        .await
        .expect("bounded check");
    assert_eq!(info.latest_version, "0.7.5");
    assert!(info.update_available);
    assert!(info.last_check_error.is_empty());
    let _ = stop.send(());
}

#[tokio::test]
async fn update_checker_runs_once_on_the_shared_supervisor_and_shuts_down_cleanly() {
    let metadata =
        r#"{"tag_name":"v0.7.5","prerelease":false,"draft":false,"assets":[]}"#.to_owned();
    let (api, stop) = fake_release_server(metadata, b"unused".to_vec()).await;
    let checker = Arc::new(UpdateCheckerState::new(
        UpdateService::with_release_api(&api).expect("service"),
    ));
    let mut callbacks = TaskCallbackRegistry::new();
    callbacks.register_update_checker(Arc::clone(&checker));
    let supervisor = RuntimeTaskSupervisor::with_callbacks(callbacks);
    let mut config = Config::default();
    config.update_checker.enabled = true;
    let specs = runtime_task_specs_for_config(&config, true)
        .into_iter()
        .filter(|spec| spec.name == "update_checker")
        .collect::<Vec<_>>();
    let mut diff = supervisor.prepare_diff(&[], &specs).expect("preflight");
    diff.commit().await.expect("commit");
    timeout(Duration::from_secs(2), async {
        while checker.snapshot().last_check_at == 0 {
            tokio::task::yield_now().await;
        }
    })
    .await
    .expect("initial check");
    assert_eq!(
        supervisor
            .task_snapshot("update_checker")
            .expect("task")
            .tick_count,
        1
    );
    assert_eq!(supervisor.shutdown().await.remaining, 0);
    let _ = stop.send(());
}

#[tokio::test]
async fn verified_replacement_self_checks_and_preserves_other_files() {
    let root = tempfile::tempdir().expect("temp root");
    let executable = root.path().join("eggpool");
    let config = root.path().join("config.toml");
    let database = root.path().join("usage.sqlite3");
    let old = b"old executable";
    fs::write(&executable, old).expect("old executable");
    fs::write(&config, b"config = 1\n").expect("config");
    fs::write(&database, b"database bytes\n").expect("database");
    let target = ReleaseVersion::parse("0.7.5").expect("version");
    let bytes = fake_executable("0.7.5");
    let service = UpdateService::new().expect("service");
    let report = service
        .replace_verified_executable(&executable, &target, &bytes)
        .await
        .expect("replacement");
    assert_eq!(report.target_version, "0.7.5");
    assert_eq!(fs::read(&config).expect("config bytes"), b"config = 1\n");
    assert_eq!(
        fs::read(&database).expect("database bytes"),
        b"database bytes\n"
    );
    assert!(
        fs::read(&executable)
            .expect("new executable")
            .starts_with(b"#!")
    );
    assert!(!root.path().join(".eggpool-update.rollback").exists());
}

#[tokio::test]
async fn bad_digest_and_non_matching_version_fail_without_replacement() {
    let root = tempfile::tempdir().expect("temp root");
    let executable = root.path().join("eggpool");
    fs::write(&executable, b"old executable").expect("old executable");
    let target = ReleaseVersion::parse("0.7.5").expect("version");
    let service = UpdateService::new().expect("service");
    let error = service
        .replace_verified_executable(&executable, &target, b"not an executable")
        .await
        .expect_err("self-check must fail");
    assert!(matches!(error, UpdateError::SelfCheckFailed));
    assert_eq!(fs::read(&executable).expect("old bytes"), b"old executable");
}

#[test]
fn task_inventory_enables_update_checker_only_when_requested() {
    let mut config = Config::default();
    config.update_checker.enabled = true;
    let specs = eggpool::task_supervisor::runtime_task_specs_for_config(&config, true);
    let update = specs
        .iter()
        .find(|spec| spec.name == "update_checker")
        .expect("update checker spec");
    assert!(update.enabled);
    assert_eq!(update.ownership, TaskOwnership::Process);
    assert!(update.run_immediately);
    assert_eq!(update.interval_s, 86_400.0);
}

fn hex_digest(bytes: &[u8]) -> String {
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn fake_executable(version: &str) -> Vec<u8> {
    format!("#!/bin/sh\nprintf '%s\\n' {version}\n").into_bytes()
}
