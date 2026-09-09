//! O009 deployment renderers, command boundaries, and uninstall safety.

use std::{fs, os::unix::fs::symlink, path::PathBuf};

use eggpool::operations::deploy::{
    CommandResult, KeepFlags, PersonalSystemdSpec, ProductionSystemdSpec, RecordedCommand,
    RecordingCommandRunner, UninstallTargets, install_cron_block, install_logrotate,
    install_systemd, merge_cron_block, render_backup_cron, render_backup_script, render_logrotate,
    render_personal_systemd, render_production_systemd, render_watchdog_cron,
    strip_managed_cron_blocks, uninstall, write_atomic,
};

#[test]
fn renderers_are_deterministic_and_quote_path_arguments() {
    let binary = PathBuf::from("/opt/Egg Pool/bin/eggpool");
    let config = PathBuf::from("/home/operator/My Config/config.toml");
    let personal = render_personal_systemd(&PersonalSystemdSpec {
        binary: binary.clone(),
        config: config.clone(),
        data_dir: PathBuf::from("/home/operator/Egg Pool/data"),
        env_file: Some(PathBuf::from("/home/operator/Egg Pool/.env")),
        user: "operator".into(),
        group: "operator".into(),
    });
    assert_eq!(
        personal,
        render_personal_systemd(&PersonalSystemdSpec {
            binary: binary.clone(),
            config: config.clone(),
            data_dir: PathBuf::from("/home/operator/Egg Pool/data"),
            env_file: Some(PathBuf::from("/home/operator/Egg Pool/.env")),
            user: "operator".into(),
            group: "operator".into(),
        })
    );
    assert!(personal.contains("ExecStart=\"/opt/Egg Pool/bin/eggpool\" --config \"/home/operator/My Config/config.toml\" serve"));
    assert!(personal.contains("EnvironmentFile=\"/home/operator/Egg Pool/.env\""));

    let cron = render_watchdog_cron(
        &binary,
        &config,
        &PathBuf::from("/home/operator/Egg Pool/eggpool.log"),
        5,
    )
    .expect("valid interval");
    assert!(cron.contains("*/5 * * * *"));
    assert!(cron.contains("'--not-a-shell-argument'") || cron.contains("'"));
    assert!(render_watchdog_cron(&binary, &config, &PathBuf::from("log"), 60).is_err());

    let production = render_production_systemd(&ProductionSystemdSpec { binary });
    assert!(production.contains("ProtectSystem=strict"));
    assert!(production.contains("User=eggpool"));
    assert!(render_logrotate(PathBuf::from("/var/log/eggpool").as_path()).contains("rotate 14"));
}

#[test]
fn cron_merge_is_idempotent_and_preserves_unrelated_lines() {
    let block =
        "# BEGIN EggPool watchdog\n*/5 * * * * eggpool ensure-running\n# END EggPool watchdog\n";
    let existing = "MAILTO=operator@example.invalid\n";
    let once = merge_cron_block(existing, block);
    let twice = merge_cron_block(&once, block);
    assert_eq!(once, twice);
    assert_eq!(
        strip_managed_cron_blocks(&once),
        "MAILTO=operator@example.invalid\n\n"
    );
}

#[test]
fn install_uses_argv_commands_and_stops_at_first_mandatory_failure() {
    let root = tempfile::tempdir().expect("temp root");
    let config = root.path().join("config.toml");
    let unit = root.path().join("systemd/eggpool.service");
    fs::write(
        &config,
        toml::to_string(&eggpool::Config::default()).expect("config"),
    )
    .expect("write config");

    let mut runner = RecordingCommandRunner::default();
    install_systemd(
        &mut runner,
        &unit,
        "[Service]\nExecStart=/tmp/eggpool serve\n",
        &config,
        &[],
        true,
    )
    .expect("install");
    assert_eq!(
        runner
            .calls
            .iter()
            .map(|call| call.program.as_str())
            .collect::<Vec<_>>(),
        vec!["systemctl", "systemctl", "systemctl"]
    );
    assert_eq!(runner.calls[0].args, vec!["daemon-reload"]);
    assert!(unit.is_file());

    let mut failing = RecordingCommandRunner {
        calls: Vec::new(),
        results: vec![CommandResult {
            status: 9,
            stdout: String::new(),
            stderr: "daemon reload failed".into(),
        }],
    };
    let error = install_logrotate(
        &mut failing,
        &root.path().join("logrotate/eggpool"),
        "logs {}\n",
        true,
    )
    .expect_err("validation failure");
    assert!(error.to_string().contains("logrotate"));
}

#[test]
fn backup_cron_is_a_real_wrapper_and_keeps_production_schedule_separate() {
    let binary = PathBuf::from("/opt/eggpool/bin/eggpool");
    let config = PathBuf::from("/etc/eggpool/config.toml");
    let script = render_backup_script(&binary, &config);
    assert!(script.starts_with("#!/bin/sh\n"));
    assert!(script.contains(" backup\n"));
    let personal = render_backup_cron(&binary, &config, false);
    let production = render_backup_cron(&binary, &config, true);
    assert!(personal.contains("BEGIN EggPool backup"));
    assert!(production.contains("0 2 * * * root '/usr/local/bin/eggpool-backup'"));
    assert!(!production.contains("BEGIN EggPool backup"));
}

#[test]
fn uninstall_removes_only_known_targets_and_honors_all_keep_flags() {
    let root = tempfile::tempdir().expect("temp root");
    let data = root.path().join("data");
    let state = root.path().join("state");
    fs::create_dir_all(&data).expect("data");
    fs::create_dir_all(&state).expect("state");
    fs::write(data.join("usage.sqlite3"), "db").expect("db");
    fs::write(state.join("eggpool.log"), "log").expect("log");
    let config = root.path().join("config.toml");
    let binary = root.path().join("bin/eggpool");
    fs::create_dir_all(binary.parent().expect("bin")).expect("bin");
    for path in [&config, &binary] {
        fs::write(path, "owned").expect("target");
    }
    let unit = root.path().join("eggpool.service");
    let logrotate = root.path().join("eggpool.logrotate");
    let cron = root.path().join("eggpool.cron");
    let script = root.path().join("eggpool-backup");
    for path in [&unit, &logrotate, &cron, &script] {
        fs::write(path, "owned").expect("artifact");
    }
    let rc = root.path().join(".profile");
    fs::write(
        &rc,
        "export PATH=\"$HOME/.local/bin/eggpool:$PATH\"\nkeep\n",
    )
    .expect("rc");
    let targets = UninstallTargets {
        binary,
        config,
        env: None,
        data_dir: data,
        state_dir: state,
        systemd_unit: unit,
        logrotate,
        production_cron: cron,
        backup_script: script,
        shell_rc_files: vec![rc.clone()],
    };
    let mut runner = RecordingCommandRunner::default();
    let leftovers =
        uninstall(&mut runner, &targets, KeepFlags::default(), true).expect("uninstall");
    assert!(leftovers.is_empty());
    let rc_contents = fs::read_to_string(&rc).expect("rc remains for unrelated settings");
    assert!(rc_contents.contains("keep"));
    assert!(!rc_contents.contains("eggpool"));

    let root = tempfile::tempdir().expect("keep root");
    let data = root.path().join("data");
    let state = root.path().join("state");
    fs::create_dir_all(&data).expect("data");
    fs::create_dir_all(&state).expect("state");
    let config = root.path().join("config.toml");
    let binary = root.path().join("eggpool");
    fs::write(&config, "config").expect("config");
    fs::write(&binary, "binary").expect("binary");
    let targets = UninstallTargets {
        binary,
        config,
        env: None,
        data_dir: data,
        state_dir: state,
        systemd_unit: root.path().join("service"),
        logrotate: root.path().join("rotate"),
        production_cron: root.path().join("cron"),
        backup_script: root.path().join("script"),
        shell_rc_files: Vec::new(),
    };
    let mut runner = RecordingCommandRunner::default();
    uninstall(
        &mut runner,
        &targets,
        KeepFlags {
            data: true,
            config: true,
            path: true,
            deploy_artifacts: true,
        },
        true,
    )
    .expect("keep uninstall");
    assert!(!targets.binary.exists());
    assert!(targets.config.exists());
    assert!(targets.data_dir.exists());
}

#[test]
fn symlinked_deployment_parent_is_refused() {
    let root = tempfile::tempdir().expect("root");
    let outside = tempfile::tempdir().expect("outside");
    let link = root.path().join("link");
    symlink(outside.path(), &link).expect("symlink");
    let error =
        write_atomic(&link.join("eggpool.service"), b"unit", 0o644).expect_err("symlink escape");
    assert!(error.to_string().contains("unsafe"));
    assert!(!outside.path().join("eggpool.service").exists());
}

#[test]
fn fake_crontab_runner_receives_stdin_as_one_argv_command() {
    let mut runner = RecordingCommandRunner::default();
    install_cron_block(
        &mut runner,
        "operator",
        "# BEGIN EggPool test\n# END EggPool test\n",
    )
    .expect("cron install");
    let writes = runner
        .calls
        .iter()
        .filter(|call: &&RecordedCommand| call.stdin.is_some())
        .collect::<Vec<_>>();
    assert_eq!(writes.len(), 1);
    assert_eq!(writes[0].program, "crontab");
    assert_eq!(writes[0].args, vec!["-u", "operator", "-"]);
}
