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
    use nix::sys::termios::{SetArg, SpecialCharacterIndices, cfmakeraw, tcgetattr, tcsetattr};
    use nix::unistd::read;
    use std::os::fd::AsFd;

    let stdin = io::stdin();
    let original = tcgetattr(&stdin).map_err(|error| SelectError::Io(io::Error::other(error)))?;
    let mut raw = original.clone();
    cfmakeraw(&mut raw);
    raw.control_chars[SpecialCharacterIndices::VMIN as usize] = 1;
    raw.control_chars[SpecialCharacterIndices::VTIME as usize] = 0;
    tcsetattr(&stdin, SetArg::TCSANOW, &raw)
        .map_err(|error| SelectError::Io(io::Error::other(error)))?;

    struct RawGuard {
        original: nix::sys::termios::Termios,
    }
    impl Drop for RawGuard {
        fn drop(&mut self) {
            let stdin = io::stdin();
            let _ = tcsetattr(&stdin, SetArg::TCSANOW, &self.original);
        }
    }
    let guard = RawGuard { original };
    let stdin_fd = stdin.as_fd();

    // Bounded wait (deciseconds) for the bytes following an Esc prefix so a
    // standalone Esc cancels promptly while arrow-key CSI sequences still
    // parse. Never blocks indefinitely after Esc.
    const ESCAPE_WAIT_DECISECONDS: u8 = 1;

    let mut selected = 0_usize;
    loop {
        let frame = render_menu(title, options, selected);
        {
            let mut stdout = io::stdout();
            stdout
                .write_all(frame.as_bytes())
                .map_err(SelectError::Io)?;
            stdout.flush().map_err(SelectError::Io)?;
        }
        let mut byte = [0_u8; 1];
        let count =
            read(stdin_fd, &mut byte).map_err(|error| SelectError::Io(io::Error::other(error)))?;
        if count == 0 {
            return Ok(None);
        }
        match byte[0] {
            0x1b => {
                let mut timed = raw.clone();
                timed.control_chars[SpecialCharacterIndices::VMIN as usize] = 0;
                timed.control_chars[SpecialCharacterIndices::VTIME as usize] =
                    ESCAPE_WAIT_DECISECONDS;
                tcsetattr(&stdin, SetArg::TCSANOW, &timed)
                    .map_err(|error| SelectError::Io(io::Error::other(error)))?;
                let mut following = Vec::new();
                for _ in 0..2 {
                    let mut next = [0_u8; 1];
                    let extra = read(stdin_fd, &mut next)
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
                tcsetattr(&stdin, SetArg::TCSANOW, &raw)
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
                    let _ = io::stdout().write_all(b"\r\n");
                    let _ = io::stdout().flush();
                    drop(guard);
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
}
