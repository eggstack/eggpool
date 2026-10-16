//! O001 parser/contract coverage.
//!
//! The operational handlers remain intentionally deferred.  This test keeps
//! the Rust parser aligned with the checked-in Python ownership matrix so a
//! later O002-O009 implementation cannot silently lose a command or option.

use std::collections::BTreeMap;

use clap::{Command, CommandFactory, Parser};
use eggpool::{Cli, Command as EggpoolCommand};
use serde_json::Value;

const MATRIX: &str = include_str!("../../tests/fixtures/cli/contract-matrix.json");

fn fixture_commands() -> BTreeMap<String, Vec<String>> {
    let value: Value = serde_json::from_str(MATRIX).expect("valid O001 matrix");
    value["commands"]
        .as_array()
        .expect("command matrix")
        .iter()
        .map(|row| {
            (
                row["path"].as_str().expect("command path").to_owned(),
                row["options"]
                    .as_array()
                    .expect("command options")
                    .iter()
                    .map(|option| option.as_str().expect("option name").to_owned())
                    .collect::<Vec<_>>(),
            )
        })
        .map(|(path, mut options)| {
            options.sort();
            (path, options)
        })
        .collect()
}

fn command_for_path<'a>(root: &'a mut Command, path: &str) -> &'a mut Command {
    let mut current = root;
    for component in path.split(' ') {
        current = current
            .find_subcommand_mut(component)
            .unwrap_or_else(|| panic!("Rust parser is missing {path}"));
    }
    current
}

fn rust_command_options(root: &mut Command, path: &str) -> Vec<String> {
    let command = command_for_path(root, path);
    let mut options = command
        .get_arguments()
        .filter_map(|argument| argument.get_long().map(str::to_owned))
        .map(|long| format!("--{long}"))
        .collect::<Vec<_>>();
    options.sort();
    options
}

#[test]
fn rust_parser_has_one_entry_for_every_frozen_python_command() {
    let expected = fixture_commands();
    assert_eq!(expected.len(), 72);
    let mut root = Cli::command();
    for (path, options) in expected {
        assert_eq!(rust_command_options(&mut root, &path), options, "{path}");
    }
}

#[test]
fn current_option_deltas_are_parseable_in_rust() {
    let dashboard = Cli::try_parse_from(["eggpool", "dashboard", "public", "--off"])
        .expect("dashboard --off is part of the current product contract");
    assert!(matches!(
        dashboard.command,
        Some(EggpoolCommand::Dashboard(_))
    ));

    let recompute = Cli::try_parse_from(["eggpool", "stats", "recompute-costs", "--apply"])
        .expect("stats recompute --apply is part of the current product contract");
    assert!(matches!(recompute.command, Some(EggpoolCommand::Stats(_))));

    let repair = Cli::try_parse_from(["eggpool", "stats", "repair-costs", "--dry-run"])
        .expect("stats repair --dry-run is part of the current product contract");
    assert!(matches!(repair.command, Some(EggpoolCommand::Stats(_))));
}

#[test]
fn mutually_exclusive_current_flags_fail_closed() {
    assert!(Cli::try_parse_from(["eggpool", "dashboard", "public", "--on", "--off",]).is_err());
    assert!(
        Cli::try_parse_from([
            "eggpool",
            "stats",
            "recompute-costs",
            "--dry-run",
            "--apply",
        ])
        .is_err()
    );
}

#[test]
fn configremote_parses_targets_and_options() {
    let codex = Cli::try_parse_from(["eggpool", "configremote", "codex"])
        .expect("configremote codex parses");
    assert!(matches!(
        codex.command,
        Some(EggpoolCommand::Configremote(_))
    ));
    let opencode = Cli::try_parse_from([
        "eggpool",
        "configremote",
        "opencode",
        "--base-url",
        "https://pool.example.internal/v1",
        "--format",
        "json",
        "--shell",
        "posix",
        "--no-bootstrap",
    ])
    .expect("configremote opencode with options parses");
    assert!(matches!(
        opencode.command,
        Some(EggpoolCommand::Configremote(_))
    ));
    // Missing target fails closed.
    assert!(Cli::try_parse_from(["eggpool", "configremote"]).is_err());
}

#[test]
fn local_model_policy_profile_commands_parse_with_exact_effects() {
    assert!(
        Cli::try_parse_from([
            "eggpool",
            "users",
            "policy",
            "create",
            "restricted",
            "Restricted",
            "deny",
        ])
        .is_ok()
    );
    assert!(
        Cli::try_parse_from([
            "eggpool",
            "users",
            "policy",
            "rule",
            "restricted",
            "model-a/provider-a",
            "allow",
        ])
        .is_ok()
    );
    assert!(
        Cli::try_parse_from([
            "eggpool",
            "users",
            "policy",
            "assign",
            "member-a",
            "restricted",
        ])
        .is_ok()
    );
    assert!(
        Cli::try_parse_from([
            "eggpool",
            "users",
            "policy",
            "rule",
            "restricted",
            "model-a",
            "maybe",
        ])
        .is_err()
    );
}

#[test]
fn local_user_budget_commands_parse_with_bounded_windows() {
    assert!(
        Cli::try_parse_from([
            "eggpool",
            "users",
            "budget",
            "set",
            "member-a",
            "--window",
            "day",
            "--max-requests",
            "25",
            "--max-tokens",
            "100000",
            "--max-concurrent",
            "2",
        ])
        .is_ok()
    );
    assert!(
        Cli::try_parse_from([
            "eggpool",
            "users",
            "budget",
            "remove",
            "member-a",
            "--model-id",
            "model-a",
            "--window",
            "month",
        ])
        .is_ok()
    );
    assert!(
        Cli::try_parse_from([
            "eggpool",
            "users",
            "budget",
            "set",
            "member-a",
            "--window",
            "rolling",
            "--max-requests",
            "25",
        ])
        .is_err()
    );
    // M008: enforcement mode is explicit and bounded; the default stays
    // estimated so existing rules are never silently promoted to hard.
    let estimated = Cli::try_parse_from([
        "eggpool",
        "users",
        "budget",
        "set",
        "member-a",
        "--window",
        "day",
        "--max-tokens",
        "100000",
    ])
    .expect("default enforcement parses");
    let rendered = format!("{:?}", estimated.command);
    assert!(
        rendered.contains("enforcement: \"estimated\""),
        "budget set defaults to estimated enforcement: {rendered}"
    );
    assert!(
        Cli::try_parse_from([
            "eggpool",
            "users",
            "budget",
            "set",
            "member-a",
            "--window",
            "day",
            "--max-tokens",
            "100000",
            "--enforcement",
            "hard",
        ])
        .is_ok()
    );
    assert!(
        Cli::try_parse_from([
            "eggpool",
            "users",
            "budget",
            "set",
            "member-a",
            "--window",
            "day",
            "--max-tokens",
            "100000",
            "--enforcement",
            "guaranteed",
        ])
        .is_err()
    );
    // M009: instance ceilings share the window/enforcement contract without
    // a user argument.
    assert!(
        Cli::try_parse_from([
            "eggpool",
            "users",
            "global-budget",
            "set",
            "--window",
            "month",
            "--max-requests",
            "1000",
            "--enforcement",
            "hard",
        ])
        .is_ok()
    );
    assert!(
        Cli::try_parse_from([
            "eggpool",
            "users",
            "global-budget",
            "remove",
            "--window",
            "month",
        ])
        .is_ok()
    );
    assert!(
        Cli::try_parse_from([
            "eggpool",
            "users",
            "global-budget",
            "set",
            "--window",
            "month",
            "--max-requests",
            "1000",
            "--enforcement",
            "guaranteed",
        ])
        .is_err()
    );
}

#[test]
fn headless_member_lifecycle_commands_parse_with_secret_safe_inputs() {
    assert!(
        Cli::try_parse_from([
            "eggpool",
            "users",
            "add",
            "member-a",
            "--password-stdin",
            "--profile-id",
            "restricted",
            "--actor",
            "admin",
        ])
        .is_ok()
    );
    assert!(Cli::try_parse_from(["eggpool", "users", "disable", "member-a"]).is_ok());
    assert!(Cli::try_parse_from(["eggpool", "users", "enable", "member-a"]).is_ok());
    assert!(Cli::try_parse_from(["eggpool", "users", "show", "member-a"]).is_ok());
    assert!(
        Cli::try_parse_from([
            "eggpool",
            "users",
            "add",
            "member-b",
            "--password-stdin",
            "--no-profile"
        ])
        .is_ok()
    );
    assert!(
        Cli::try_parse_from([
            "eggpool",
            "users",
            "add",
            "member-b",
            "--password-stdin",
            "--no-profile",
            "--profile-id",
            "restricted"
        ])
        .is_err()
    );
    assert!(Cli::try_parse_from(["eggpool", "users", "defaults", "show"]).is_ok());
    assert!(Cli::try_parse_from(["eggpool", "users", "defaults", "set", "restricted"]).is_ok());
    assert!(Cli::try_parse_from(["eggpool", "users", "defaults", "clear"]).is_ok());
    assert!(
        Cli::try_parse_from([
            "eggpool",
            "users",
            "add",
            "member-a",
            "--password",
            "plaintext-in-argv",
        ])
        .is_err()
    );
}
