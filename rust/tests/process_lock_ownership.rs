//! Process lock ownership and stale-removal race coverage.
//!
//! The watchdog guard may only be removed when the lock file was read
//! successfully and its owner was confirmed absent, or when the lock file
//! named no owner at all. An undeterminable owner is a held lock, and a lock
//! published by another process after the ownership check is never deleted.
//!
//! An empty lock file names no owner: a publisher creates the file before it
//! writes its process id into it. Such a file is a publication that never
//! completed, so it must not wedge the watchdog forever. Reclaiming it is
//! safe because the publisher confirms that the path still names its own
//! file after it writes, and a reclaim that raced a live publisher restores
//! the lock instead of removing it.

use std::{fs, path::Path};

use eggpool::operations::{
    paths::RuntimePaths,
    process::{
        ProcessError, acquire_start_guard, acquire_start_guard_with_observer,
        acquire_start_guard_with_seams,
    },
};

/// A process id that is not running on any supported host.
const DEAD_PID: &str = "99999999";

fn paths(root: &Path) -> RuntimePaths {
    RuntimePaths {
        config_path: root.join("config.toml"),
        config_dir: root.join("config"),
        data_dir: root.join("data"),
        state_dir: root.join("state"),
        env_path: None,
        runtime_dir: root.join("runtime"),
        pid_file: root.join("state/eggpool.pid"),
        log_file: root.join("state/eggpool.log"),
        control_socket: root.join("runtime/eggpool.sock"),
    }
}

fn prepare(runtime_paths: &RuntimePaths) {
    fs::create_dir_all(&runtime_paths.state_dir).expect("state directory");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&runtime_paths.state_dir, fs::Permissions::from_mode(0o700))
            .expect("private state directory");
    }
}

fn lock_path(runtime_paths: &RuntimePaths) -> std::path::PathBuf {
    runtime_paths.state_dir.join("eggpool.ensure-running.lock")
}

#[test]
fn unparsable_lock_file_is_a_held_lock_and_is_left_in_place() {
    let root = tempfile::tempdir().expect("temp root");
    let runtime_paths = paths(root.path());
    prepare(&runtime_paths);
    let path = lock_path(&runtime_paths);
    fs::write(&path, b"not-a-process-id").expect("unparsable lock");

    let outcome = acquire_start_guard(&runtime_paths);
    assert!(matches!(outcome, Err(ProcessError::LockHeld)));
    assert_eq!(
        fs::read_to_string(&path).expect("lock remains").trim(),
        "not-a-process-id"
    );
}

#[test]
fn unreadable_lock_file_is_a_held_lock_and_is_left_in_place() {
    let root = tempfile::tempdir().expect("temp root");
    let runtime_paths = paths(root.path());
    prepare(&runtime_paths);
    let path = lock_path(&runtime_paths);
    fs::write(&path, DEAD_PID).expect("lock file");
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&path, fs::Permissions::from_mode(0o000)).expect("locked permissions");
    }
    // A privileged reader can read the file regardless of its mode, so the
    // unreadable case is only observable for an unprivileged reader.
    if fs::read_to_string(&path).is_ok() {
        return;
    }

    let outcome = acquire_start_guard(&runtime_paths);
    assert!(matches!(outcome, Err(ProcessError::LockHeld)));
    assert!(path.exists(), "the unreadable lock file is left in place");
}

#[test]
fn an_ownerless_lock_file_is_reclaimed_and_the_acquisition_proceeds() {
    let root = tempfile::tempdir().expect("temp root");
    let runtime_paths = paths(root.path());
    prepare(&runtime_paths);
    let path = lock_path(&runtime_paths);
    // A publication that never completed: the file exists but names no
    // owner. It must not wedge the watchdog forever.
    fs::write(&path, b"").expect("ownerless lock");

    let guard = acquire_start_guard(&runtime_paths).expect("ownerless lock is reclaimed");
    assert_eq!(
        fs::read_to_string(&path).expect("owned lock").trim(),
        std::process::id().to_string(),
        "the reclaimed lock is republished by this process"
    );
    drop(guard);
    assert!(!path.exists(), "the released lock is removed");
}

#[test]
fn an_owner_published_during_the_ownerless_reclaim_is_held_and_restored() {
    let root = tempfile::tempdir().expect("temp root");
    let runtime_paths = paths(root.path());
    prepare(&runtime_paths);
    let path = lock_path(&runtime_paths);
    fs::write(&path, b"").expect("ownerless lock");

    let owner = std::process::id();
    let outcome = acquire_start_guard_with_seams(
        &runtime_paths,
        |lock| {
            // A competing publisher completes its publication while the
            // ownerless reclaim is being decided. The reclaim must find the
            // published owner and put the lock back instead of removing it.
            fs::write(lock, owner.to_string()).expect("published owner");
        },
        |_| {},
    );

    assert!(matches!(outcome, Err(ProcessError::LockHeld)));
    assert_eq!(
        fs::read_to_string(&path).expect("lock remains").trim(),
        owner.to_string(),
        "the published owner must not be removed"
    );
}

#[test]
fn a_publisher_that_loses_the_lock_path_does_not_claim_ownership() {
    let root = tempfile::tempdir().expect("temp root");
    let runtime_paths = paths(root.path());
    prepare(&runtime_paths);

    let outcome = acquire_start_guard_with_seams(
        &runtime_paths,
        |_| {},
        |lock| {
            // A reclaim moved the freshly published lock aside before the
            // publisher could confirm that the path still names its file.
            // The publisher must not enter the spawn section.
            let moved = lock.with_file_name("moved.lock");
            fs::rename(lock, &moved).expect("lock moved aside");
        },
    );

    assert!(
        matches!(outcome, Err(ProcessError::LockHeld)),
        "a publisher that lost the path must not claim ownership"
    );
}

#[test]
fn a_lock_that_vanishes_during_the_stale_decision_is_not_reported_as_held() {
    let root = tempfile::tempdir().expect("temp root");
    let runtime_paths = paths(root.path());
    prepare(&runtime_paths);
    let path = lock_path(&runtime_paths);
    fs::write(&path, DEAD_PID).expect("stale lock");

    let outcome = acquire_start_guard_with_observer(&runtime_paths, |lock| {
        // The stale lock is released by another process while the stale
        // decision is being made. The lock is free, so the acquisition must
        // proceed instead of reporting a held lock.
        fs::remove_file(lock).expect("lock released");
    });

    let guard = outcome.expect("a vanished lock is not a held lock");
    assert_eq!(
        fs::read_to_string(&path).expect("owned lock").trim(),
        std::process::id().to_string()
    );
    drop(guard);
}

#[test]
fn a_lock_published_during_the_stale_decision_is_held_and_not_removed() {
    let root = tempfile::tempdir().expect("temp root");
    let runtime_paths = paths(root.path());
    prepare(&runtime_paths);
    let path = lock_path(&runtime_paths);
    fs::write(&path, DEAD_PID).expect("stale lock");

    let owner = std::process::id();
    let outcome = acquire_start_guard_with_observer(&runtime_paths, |lock| {
        // A competing start publishes its own lock while the stale decision
        // is being made.  The publication replaces the stale file, so the
        // contender must not remove what it finds.
        let published = lock.with_file_name("concurrent.lock");
        fs::write(&published, owner.to_string()).expect("concurrent lock");
        fs::rename(&published, lock).expect("concurrent lock publish");
    });

    assert!(matches!(outcome, Err(ProcessError::LockHeld)));
    assert_eq!(
        fs::read_to_string(&path).expect("lock remains").trim(),
        owner.to_string(),
        "the concurrently published lock must not be removed"
    );
}

#[test]
fn a_genuine_stale_lock_still_recovers() {
    let root = tempfile::tempdir().expect("temp root");
    let runtime_paths = paths(root.path());
    prepare(&runtime_paths);
    let path = lock_path(&runtime_paths);
    fs::write(&path, DEAD_PID).expect("stale lock");

    let guard = acquire_start_guard(&runtime_paths).expect("stale lock recovers");
    assert_eq!(
        fs::read_to_string(&path).expect("owned lock").trim(),
        std::process::id().to_string()
    );
    drop(guard);
    assert!(!path.exists(), "the released lock is removed");
}

#[test]
fn a_live_lock_owner_is_still_a_held_lock() {
    let root = tempfile::tempdir().expect("temp root");
    let runtime_paths = paths(root.path());
    prepare(&runtime_paths);

    let guard = acquire_start_guard(&runtime_paths).expect("first guard");
    assert!(matches!(
        acquire_start_guard(&runtime_paths),
        Err(ProcessError::LockHeld)
    ));
    drop(guard);
}
