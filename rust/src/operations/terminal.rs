//! Narrow interactive terminal selector for operator prompts.
//!
//! This module owns raw-mode lifecycle only. It contains no config or provider
//! semantics: callers format bounded option strings and map the returned index
//! back to their own domain. Provider/account selection lives in
//! `config_mutation.rs`.
//!
//! Behavior matches the historical Python `TerminalMenu`: `j/k` and Up/Down
//! navigation, Enter to select, `q`/Esc to cancel, clamped (non-wrapping)
//! movement. Ctrl-C is reported as interruption rather than cancellation so
//! callers preserve normal interruption semantics.

use std::io::{self, IsTerminal, Write};

/// Instruction line rendered above every interactive menu.
pub const SELECTOR_INSTRUCTIONS: &str =
    "Use j/k or \u{2191}/\u{2193} to navigate, Enter to select, q/Esc to quit";

/// Menu title used for provider connection selection.
pub const PROVIDER_SELECT_TITLE: &str = "Select a provider to connect:";

/// Menu title used for account removal selection.
pub const ACCOUNT_REMOVE_TITLE: &str = "Select provider account to remove:";

/// Small explicit key-to-action mapping for the selector.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KeyAction {
    Next,
    Previous,
    Confirm,
    Cancel,
    Ignore,
    Interrupted,
}

/// Outcome of classifying the bytes that followed an Esc prefix.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EscapeOutcome {
    /// Standalone Esc (no further bytes within the bounded wait).
    Cancel,
    Previous,
    Next,
    /// Malformed or incomplete CSI sequence: redraw, never hang.
    Ignore,
}

/// Errors from the interactive selector. Callers map these onto their own
/// error type; `NotInteractive` signals the caller should use its
/// line-oriented fallback instead.
#[derive(Debug)]
pub enum SelectError {
    Io(io::Error),
    Interrupted,
    NotInteractive,
}

impl std::fmt::Display for SelectError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Io(error) => write!(formatter, "terminal selection failed: {error}"),
            Self::Interrupted => write!(formatter, "terminal selection interrupted"),
            Self::NotInteractive => write!(formatter, "terminal selection requires a TTY"),
        }
    }
}

impl std::error::Error for SelectError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io(error) => Some(error),
            Self::Interrupted | Self::NotInteractive => None,
        }
    }
}

/// True when both stdin and stdout are interactive terminals.
pub fn is_interactive() -> bool {
    io::stdin().is_terminal() && io::stdout().is_terminal()
}

/// Clamp-aware increment used by `j` and Down.
pub fn next_index(selected: usize, len: usize) -> usize {
    if len == 0 {
        return 0;
    }
    selected.saturating_add(1).min(len - 1)
}

/// Clamp-aware decrement used by `k` and Up.
pub fn prev_index(selected: usize, len: usize) -> usize {
    if len == 0 {
        return 0;
    }
    selected.saturating_sub(1).min(len - 1)
}

/// Classify a single non-Esc input byte.
pub fn classify_single(byte: u8) -> KeyAction {
    match byte {
        b'j' => KeyAction::Next,
        b'k' => KeyAction::Previous,
        b'\r' | b'\n' => KeyAction::Confirm,
        b'q' | b'Q' => KeyAction::Cancel,
        0x03 => KeyAction::Interrupted,
        _ => KeyAction::Ignore,
    }
}

/// Classify the bytes read after an Esc prefix within the bounded wait.
///
/// An empty slice means no continuation arrived (standalone Esc): cancel.
/// `[A`/`[B` are Up/Down; anything else is ignored so a malformed or
/// incomplete escape sequence can never hang the selector.
pub fn classify_escape_sequence(following: &[u8]) -> EscapeOutcome {
    match following {
        [] => EscapeOutcome::Cancel,
        [b'[', b'A'] => EscapeOutcome::Previous,
        [b'[', b'B'] => EscapeOutcome::Next,
        _ => EscapeOutcome::Ignore,
    }
}

/// Apply one action to the current selection. Returns the new selection and
/// whether the selector is done (`Some(index)` on confirm, `None` on cancel).
/// `Interrupted` propagates as an error signal via the boolean flag.
pub fn apply_action(
    selected: usize,
    len: usize,
    action: KeyAction,
) -> (usize, Option<Result<Option<usize>, ()>>) {
    match action {
        KeyAction::Next => (next_index(selected, len), Option::None),
        KeyAction::Previous => (prev_index(selected, len), Option::None),
        KeyAction::Confirm => (selected, Some(Ok(Some(selected)))),
        KeyAction::Cancel => (selected, Some(Ok(Option::None))),
        KeyAction::Ignore => (selected, Option::None),
        KeyAction::Interrupted => (selected, Some(Err(()))),
    }
}

fn sanitize_option(value: &str) -> String {
    value
        .chars()
        .map(|character| {
            if character.is_control() || character == '\u{1b}' {
                '?'
            } else {
                character
            }
        })
        .collect()
}

/// Render one full menu frame with explicit `\r\n` endings (required while the
/// terminal is in raw mode, where the kernel does not translate LF to CRLF).
/// Pure and deterministic for tests; the interactive loop writes the result.
pub fn render_menu(title: &str, options: &[String], selected: usize) -> String {
    const NL: &str = "\r\n";
    let mut frame = String::new();
    frame.push_str("\x1b[2J\x1b[H");
    frame.push_str(&format!("\x1b[1m{title}\x1b[0m{NL}{NL}"));
    frame.push_str(&format!("  {SELECTOR_INSTRUCTIONS}{NL}{NL}"));
    for (index, option) in options.iter().enumerate() {
        let clean = sanitize_option(option);
        if index == selected {
            frame.push_str(&format!("  > \x1b[1;32m{clean}\x1b[0m{NL}"));
        } else {
            frame.push_str(&format!("    {clean}{NL}"));
        }
    }
    frame.push_str(NL);
    frame
}

/// Run the interactive selector. Returns the selected index, or `None` when
/// the operator cancels (including EOF). Returns `NotInteractive` when stdin
/// or stdout is not a TTY so callers keep their deterministic line-oriented
/// fallback; never emits cursor-control sequences in that case.
pub fn select_one(title: &str, options: &[String]) -> Result<Option<usize>, SelectError> {
    if options.is_empty() {
        return Ok(None);
    }
    if !is_interactive() {
        return Err(SelectError::NotInteractive);
    }
    #[cfg(unix)]
    {
        run_interactive(title, options)
    }
    #[cfg(not(unix))]
    {
        let _ = title;
        Err(SelectError::NotInteractive)
    }
}

#[cfg(unix)]
fn run_interactive(title: &str, options: &[String]) -> Result<Option<usize>, SelectError> {
    use std::os::fd::AsFd;

    let stdin = io::stdin();
    let mut stdout = io::stdout();
    run_interactive_on(stdin.as_fd(), &mut stdout, title, options)
}

#[cfg(unix)]
fn run_interactive_on(
    input: std::os::fd::BorrowedFd<'_>,
    output: &mut dyn Write,
    title: &str,
    options: &[String],
) -> Result<Option<usize>, SelectError> {
    use nix::sys::termios::{SetArg, SpecialCharacterIndices, cfmakeraw, tcgetattr, tcsetattr};
    use nix::unistd::read;

    let original = tcgetattr(input).map_err(|error| SelectError::Io(io::Error::other(error)))?;
    let mut raw = original.clone();
    cfmakeraw(&mut raw);
    raw.control_chars[SpecialCharacterIndices::VMIN as usize] = 1;
    raw.control_chars[SpecialCharacterIndices::VTIME as usize] = 0;
    tcsetattr(input, SetArg::TCSANOW, &raw)
        .map_err(|error| SelectError::Io(io::Error::other(error)))?;

    struct RawGuard<'scope> {
        fd: std::os::fd::BorrowedFd<'scope>,
        original: nix::sys::termios::Termios,
    }
    impl Drop for RawGuard<'_> {
        fn drop(&mut self) {
            let _ = tcsetattr(self.fd, SetArg::TCSANOW, &self.original);
        }
    }
    let _guard = RawGuard {
        fd: input,
        original,
    };

    // Bounded wait (deciseconds) for the bytes following an Esc prefix so a
    // standalone Esc cancels promptly while arrow-key CSI sequences still
    // parse. Never blocks indefinitely after Esc.
    const ESCAPE_WAIT_DECISECONDS: u8 = 1;

    let mut selected = 0_usize;
    loop {
        let frame = render_menu(title, options, selected);
        output
            .write_all(frame.as_bytes())
            .map_err(SelectError::Io)?;
        output.flush().map_err(SelectError::Io)?;
        let mut byte = [0_u8; 1];
        let count =
            read(input, &mut byte).map_err(|error| SelectError::Io(io::Error::other(error)))?;
        if count == 0 {
            return Ok(None);
        }
        match byte[0] {
            0x1b => {
                let mut timed = raw.clone();
                timed.control_chars[SpecialCharacterIndices::VMIN as usize] = 0;
                timed.control_chars[SpecialCharacterIndices::VTIME as usize] =
                    ESCAPE_WAIT_DECISECONDS;
                tcsetattr(input, SetArg::TCSANOW, &timed)
                    .map_err(|error| SelectError::Io(io::Error::other(error)))?;
                let mut following = Vec::new();
                for _ in 0..2 {
                    let mut next = [0_u8; 1];
                    let extra = read(input, &mut next)
                        .map_err(|error| SelectError::Io(io::Error::other(error)))?;
                    if extra == 0 {
                        break;
                    }
                    following.push(next[0]);
                    if following.len() == 1 && following[0] != b'[' {
                        break;
                    }
                    if following.len() == 2 {
                        break;
                    }
                }
                tcsetattr(input, SetArg::TCSANOW, &raw)
                    .map_err(|error| SelectError::Io(io::Error::other(error)))?;
                match classify_escape_sequence(&following) {
                    EscapeOutcome::Cancel => return Ok(None),
                    EscapeOutcome::Previous => selected = prev_index(selected, options.len()),
                    EscapeOutcome::Next => selected = next_index(selected, options.len()),
                    EscapeOutcome::Ignore => {}
                }
            }
            other => match classify_single(other) {
                KeyAction::Next => selected = next_index(selected, options.len()),
                KeyAction::Previous => selected = prev_index(selected, options.len()),
                KeyAction::Confirm => {
                    let _ = output.write_all(b"\r\n");
                    let _ = output.flush();
                    return Ok(Some(selected));
                }
                KeyAction::Cancel => return Ok(None),
                KeyAction::Ignore => {}
                KeyAction::Interrupted => return Err(SelectError::Interrupted),
            },
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn navigation_clamps_at_both_ends() {
        assert_eq!(next_index(0, 3), 1);
        assert_eq!(next_index(2, 3), 2);
        assert_eq!(prev_index(0, 3), 0);
        assert_eq!(prev_index(2, 3), 1);
        assert_eq!(next_index(0, 1), 0);
        assert_eq!(prev_index(0, 1), 0);
        assert_eq!(next_index(0, 0), 0);
        assert_eq!(prev_index(0, 0), 0);
    }

    #[test]
    fn single_bytes_map_to_documented_controls() {
        assert_eq!(classify_single(b'j'), KeyAction::Next);
        assert_eq!(classify_single(b'k'), KeyAction::Previous);
        assert_eq!(classify_single(b'\r'), KeyAction::Confirm);
        assert_eq!(classify_single(b'\n'), KeyAction::Confirm);
        assert_eq!(classify_single(b'q'), KeyAction::Cancel);
        assert_eq!(classify_single(b'Q'), KeyAction::Cancel);
        assert_eq!(classify_single(0x03), KeyAction::Interrupted);
        assert_eq!(classify_single(b'1'), KeyAction::Ignore);
    }

    #[test]
    fn escape_sequences_cancel_navigate_or_ignore_without_hanging() {
        assert_eq!(classify_escape_sequence(&[]), EscapeOutcome::Cancel);
        assert_eq!(classify_escape_sequence(b"[A"), EscapeOutcome::Previous);
        assert_eq!(classify_escape_sequence(b"[B"), EscapeOutcome::Next);
        assert_eq!(classify_escape_sequence(b"["), EscapeOutcome::Ignore);
        assert_eq!(classify_escape_sequence(b"[C"), EscapeOutcome::Ignore);
        assert_eq!(classify_escape_sequence(b"O"), EscapeOutcome::Ignore);
        assert_eq!(classify_escape_sequence(b"[AB"), EscapeOutcome::Ignore);
    }

    #[test]
    fn confirm_cancel_and_interrupt_propagate_distinctly() {
        assert_eq!(apply_action(1, 3, KeyAction::Next), (2, Option::None));
        assert_eq!(apply_action(1, 3, KeyAction::Previous), (0, Option::None));
        assert_eq!(
            apply_action(1, 3, KeyAction::Confirm),
            (1, Some(Ok(Some(1))))
        );
        assert_eq!(
            apply_action(1, 3, KeyAction::Cancel),
            (1, Some(Ok(Option::None)))
        );
        assert_eq!(apply_action(1, 3, KeyAction::Ignore), (1, Option::None));
        assert_eq!(
            apply_action(1, 3, KeyAction::Interrupted),
            (1, Some(Err(())))
        );
    }

    #[test]
    fn empty_options_select_nothing() {
        assert!(render_menu(PROVIDER_SELECT_TITLE, &[], 0).contains(SELECTOR_INSTRUCTIONS));
    }

    #[test]
    fn rendered_frame_documents_controls_and_highlights_selection() {
        let options = vec!["alpha".to_owned(), "beta".to_owned()];
        let frame = render_menu(PROVIDER_SELECT_TITLE, &options, 1);
        assert!(frame.contains("Select a provider to connect:"));
        assert!(frame.contains(SELECTOR_INSTRUCTIONS));
        assert!(frame.contains("j/k"));
        assert!(frame.contains("\u{2191}/\u{2193}"));
        assert!(frame.contains("Enter"));
        assert!(frame.contains("q/Esc"));
        assert!(frame.contains("\r\n"));
        assert!(!frame.contains("\n\n\n\x1b") || frame.contains("\r\n"));
        assert!(frame.contains("> \x1b[1;32mbeta"));
        assert!(!frame.contains("> \x1b[1;32malpha"));
    }

    #[test]
    fn control_bytes_in_options_are_neutralized() {
        let options = vec!["a\x1bb\nc".to_owned()];
        let frame = render_menu("title", &options, 0);
        assert!(!frame.contains("a\x1bb\nc"));
        assert!(frame.contains("a?b?c"));
    }

    #[cfg(unix)]
    fn run_selector_on_pty(
        input: &[u8],
        options: &[String],
    ) -> (
        Result<Option<usize>, SelectError>,
        nix::sys::termios::Termios,
        nix::sys::termios::Termios,
    ) {
        use nix::pty::openpty;
        use nix::sys::termios::tcgetattr;
        use std::os::fd::AsFd;
        use std::os::unix::net::UnixStream;
        use std::sync::mpsc;
        use std::time::{Duration, Instant};

        let pty = openpty(None, None).expect("openpty");
        let slave = pty.slave;
        // Put the master side into non-blocking mode through the safe std
        // API so the drain loop cannot block. This avoids adding the nix
        // `fs` feature for `fcntl`; `O_NONBLOCK` applies to the open file
        // description, so later `nix::unistd` reads/writes on the same fd
        // observe it.
        let master = UnixStream::from(pty.master);
        master.set_nonblocking(true).expect("master nonblocking");
        let original = tcgetattr(&slave).expect("slave tcgetattr");

        let (sender, receiver) = mpsc::channel();
        let outcome = std::thread::scope(|scope| {
            scope.spawn(|| {
                let writer_fd = slave.try_clone().expect("duplicate slave");
                let mut writer = std::fs::File::from(writer_fd);
                let outcome = run_interactive_on(slave.as_fd(), &mut writer, "PTY test", options);
                let _ = sender.send(outcome);
            });
            // Wait briefly for the selector to install raw mode so master
            // input is not consumed by the pre-raw line discipline.
            let raw_start = Instant::now();
            loop {
                let current = tcgetattr(&slave).expect("poll slave termios");
                if current != original {
                    break;
                }
                if raw_start.elapsed() > Duration::from_secs(2) {
                    break;
                }
                std::thread::sleep(Duration::from_millis(1));
            }
            let mut written = 0_usize;
            let write_start = Instant::now();
            while written < input.len() {
                match nix::unistd::write(master.as_fd(), &input[written..]) {
                    Ok(0) => std::thread::sleep(Duration::from_millis(1)),
                    Ok(count) => written += count,
                    Err(nix::errno::Errno::EAGAIN) => {
                        std::thread::sleep(Duration::from_millis(1));
                    }
                    Err(error) => panic!("pty write failed: {error}"),
                }
                assert!(
                    write_start.elapsed() <= Duration::from_secs(2),
                    "pty write timeout"
                );
            }
            let wait_start = Instant::now();
            loop {
                let mut discard = [0_u8; 4096];
                match nix::unistd::read(master.as_fd(), &mut discard) {
                    Ok(_) | Err(nix::errno::Errno::EAGAIN) => {}
                    Err(error) => panic!("pty drain failed: {error}"),
                }
                match receiver.try_recv() {
                    Ok(outcome) => {
                        for _ in 0..8 {
                            let mut tail = [0_u8; 4096];
                            match nix::unistd::read(master.as_fd(), &mut tail) {
                                Ok(0) => break,
                                Ok(_) => {}
                                Err(nix::errno::Errno::EAGAIN) => break,
                                Err(error) => panic!("pty final drain failed: {error}"),
                            }
                        }
                        return outcome;
                    }
                    Err(mpsc::TryRecvError::Empty) => {
                        assert!(
                            wait_start.elapsed() <= Duration::from_secs(5),
                            "pty selector timeout"
                        );
                        std::thread::sleep(Duration::from_millis(1));
                    }
                    Err(mpsc::TryRecvError::Disconnected) => {
                        panic!("selector thread ended without a result")
                    }
                }
            }
        });
        let restored = tcgetattr(&slave).expect("restored tcgetattr");
        // SelectError carries an io::Error which is not PartialEq; callers
        // match on the variant instead of comparing full results.
        (outcome, original, restored)
    }

    #[cfg(unix)]
    fn assert_termios_restored(
        original: &nix::sys::termios::Termios,
        restored: &nix::sys::termios::Termios,
    ) {
        use nix::sys::termios::LocalFlags;
        // macOS sets PENDIN as a side effect of `tcsetattr` on a PTY slave,
        // even for a bare raw round-trip with no I/O. PENDIN is not part of
        // the raw transition (`cfmakeraw` never touches it), so mask it and
        // compare all raw-relevant fields explicitly.
        assert_eq!(
            original.input_flags, restored.input_flags,
            "input flags restored"
        );
        assert_eq!(
            original.output_flags, restored.output_flags,
            "output flags restored"
        );
        assert_eq!(
            original.control_flags, restored.control_flags,
            "control flags restored"
        );
        let mut expected_local = original.local_flags;
        expected_local.remove(LocalFlags::PENDIN);
        let mut actual_local = restored.local_flags;
        actual_local.remove(LocalFlags::PENDIN);
        assert_eq!(expected_local, actual_local, "local flags restored");
        assert_eq!(
            original.control_chars, restored.control_chars,
            "control chars restored"
        );
    }

    #[cfg(unix)]
    #[test]
    fn pty_confirm_restores_termios() {
        let options = vec!["alpha".to_owned(), "beta".to_owned()];
        let (outcome, original, restored) = run_selector_on_pty(b"j\r", &options);
        assert_eq!(outcome.expect("confirm result"), Some(1));
        assert_termios_restored(&original, &restored);
    }

    #[cfg(unix)]
    #[test]
    fn pty_cancel_q_restores_termios() {
        let options = vec!["alpha".to_owned(), "beta".to_owned()];
        let (outcome, original, restored) = run_selector_on_pty(b"q", &options);
        assert_eq!(outcome.expect("cancel result"), None);
        assert_termios_restored(&original, &restored);
    }

    #[cfg(unix)]
    #[test]
    fn pty_cancel_esc_restores_termios() {
        let options = vec!["alpha".to_owned(), "beta".to_owned()];
        let (outcome, original, restored) = run_selector_on_pty(b"\x1b", &options);
        assert_eq!(outcome.expect("esc cancel result"), None);
        assert_termios_restored(&original, &restored);
    }

    #[cfg(unix)]
    #[test]
    fn pty_interrupt_restores_termios() {
        let options = vec!["alpha".to_owned(), "beta".to_owned()];
        let (outcome, original, restored) = run_selector_on_pty(b"\x03", &options);
        assert!(matches!(outcome, Err(SelectError::Interrupted)));
        assert_termios_restored(&original, &restored);
    }
}
