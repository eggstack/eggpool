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
    assert_eq!(expected.len(), 63);
    let mut root = Cli::command();
    for (path, options) in expected {
        assert_eq!(rust_command_options(&mut root, &path), options, "{path}");
    }
}

#[test]
fn current_python_option_deltas_are_parseable_in_rust() {
    let dashboard = Cli::try_parse_from(["eggpool", "dashboard", "public", "--off"])
        .expect("dashboard --off is part of the current Python contract");
    assert!(matches!(
        dashboard.command,
        Some(EggpoolCommand::Dashboard(_))
    ));

    let recompute = Cli::try_parse_from(["eggpool", "stats", "recompute-costs", "--apply"])
        .expect("stats recompute --apply is part of the current Python contract");
    assert!(matches!(recompute.command, Some(EggpoolCommand::Stats(_))));

    let repair = Cli::try_parse_from(["eggpool", "stats", "repair-costs", "--dry-run"])
        .expect("stats repair --dry-run is part of the current Python contract");
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
