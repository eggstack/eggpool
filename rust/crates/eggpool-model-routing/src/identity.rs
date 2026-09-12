use std::fmt;

use sha2::{Digest, Sha256};

pub const AFFINITY_SESSION_HEADER_MAX_BYTES: usize = 512;
pub const AUTOMATIC_PREFIX_MAX_BYTES: usize = 4_096;
pub const AUTOMATIC_FIRST_USER_MIN_BYTES: usize = 1_536;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SessionSource {
    ExplicitSession,
    AutomaticSession,
}

#[derive(Clone, PartialEq, Eq)]
pub struct SessionIdentity {
    pub digest: [u8; 32],
    pub source: SessionSource,
}

impl fmt::Debug for SessionIdentity {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("SessionIdentity")
            .field(
                "digest",
                &self
                    .digest
                    .iter()
                    .map(|byte| format!("{byte:02x}"))
                    .collect::<String>(),
            )
            .field("source", &self.source)
            .finish()
    }
}

pub fn session_identity_from_header(value: Option<&str>) -> Option<SessionIdentity> {
    let value = value?;
    if value.is_empty()
        || value.len() > AFFINITY_SESSION_HEADER_MAX_BYTES
        || value
            .chars()
            .any(|character| (character as u32) < 32 || character == '\u{7f}')
    {
        return None;
    }
    let digest = Sha256::digest(value.as_bytes());
    Some(SessionIdentity {
        digest: digest.into(),
        source: SessionSource::ExplicitSession,
    })
}

#[derive(Clone, PartialEq, Eq)]
pub struct ConversationTextFragment {
    pub role: String,
    pub text: String,
}

impl fmt::Debug for ConversationTextFragment {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ConversationTextFragment")
            .field("role", &self.role)
            .field("text_bytes", &self.text.len())
            .finish()
    }
}

impl ConversationTextFragment {
    pub fn new(role: impl Into<String>, text: impl Into<String>) -> Self {
        Self {
            role: role.into(),
            text: text.into(),
        }
    }
}

#[derive(Clone, Default, PartialEq, Eq)]
pub struct ConversationPrefix {
    pub system_developer: Vec<ConversationTextFragment>,
    pub first_user_text: Option<String>,
}

impl fmt::Debug for ConversationPrefix {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ConversationPrefix")
            .field("system_developer", &self.system_developer)
            .field(
                "first_user_text_bytes",
                &self.first_user_text.as_ref().map(String::len),
            )
            .finish()
    }
}

impl ConversationPrefix {
    pub fn new(
        system_developer: Vec<ConversationTextFragment>,
        first_user_text: Option<String>,
    ) -> Self {
        Self {
            system_developer,
            first_user_text,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AffinityIdentityInput {
    pub client_surface: String,
    pub explicit_session: Option<SessionIdentity>,
    pub conversation_prefix: Option<ConversationPrefix>,
}

impl AffinityIdentityInput {
    pub fn explicit(client_surface: impl Into<String>, identity: SessionIdentity) -> Self {
        Self {
            client_surface: client_surface.into(),
            explicit_session: Some(identity),
            conversation_prefix: None,
        }
    }

    pub fn automatic(client_surface: impl Into<String>, prefix: ConversationPrefix) -> Self {
        Self {
            client_surface: client_surface.into(),
            explicit_session: None,
            conversation_prefix: Some(prefix),
        }
    }

    pub fn session_identity(&self) -> Option<SessionIdentity> {
        self.explicit_session.clone().or_else(|| {
            self.conversation_prefix
                .as_ref()
                .and_then(|prefix| automatic_session_identity(prefix, &self.client_surface))
        })
    }
}

fn normalize_identity_text(value: &str) -> String {
    value.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn bounded_utf8(value: &str, max_bytes: usize) -> Vec<u8> {
    if max_bytes == 0 {
        return Vec::new();
    }
    let bytes = value.as_bytes();
    if bytes.len() <= max_bytes {
        return bytes.to_vec();
    }
    let head_budget = max_bytes / 2;
    let tail_budget = max_bytes - head_budget;
    let mut head_end = head_budget;
    while head_end > 0 && !value.is_char_boundary(head_end) {
        head_end -= 1;
    }
    let mut tail_start = bytes.len() - tail_budget;
    while tail_start < bytes.len() && !value.is_char_boundary(tail_start) {
        tail_start += 1;
    }
    bytes[..head_end]
        .iter()
        .chain(bytes[tail_start..].iter())
        .copied()
        .collect()
}

fn bounded_identity_field(role: &str, text: &str, max_bytes: usize) -> Vec<u8> {
    let role_bytes = role.as_bytes();
    let framing_bytes = 2 + role_bytes.len() + 4;
    if max_bytes <= framing_bytes {
        return Vec::new();
    }
    let text_bytes = bounded_utf8(text, max_bytes - framing_bytes);
    if text_bytes.is_empty() {
        return Vec::new();
    }
    let mut field = Vec::with_capacity(framing_bytes + text_bytes.len());
    field.extend((role_bytes.len() as u16).to_be_bytes());
    field.extend(role_bytes);
    field.extend((text_bytes.len() as u32).to_be_bytes());
    field.extend(text_bytes);
    field
}

pub fn automatic_session_identity(
    prefix: &ConversationPrefix,
    client_surface: &str,
) -> Option<SessionIdentity> {
    if client_surface == "responses" {
        return None;
    }
    let user_text = normalize_identity_text(prefix.first_user_text.as_deref()?);
    if user_text.is_empty() {
        return None;
    }
    let mut digest = Sha256::new();
    digest.update(b"eggpool-route-affinity/v1");
    digest.update((client_surface.len() as u16).to_be_bytes());
    digest.update(client_surface.as_bytes());
    let user_role_overhead = 2 + 4 + 4;
    let reserved_user_bytes = AUTOMATIC_FIRST_USER_MIN_BYTES
        .min(AUTOMATIC_PREFIX_MAX_BYTES.saturating_sub(user_role_overhead));
    let mut remaining = AUTOMATIC_PREFIX_MAX_BYTES;
    let mut system_budget = AUTOMATIC_PREFIX_MAX_BYTES
        .saturating_sub(user_role_overhead)
        .saturating_sub(reserved_user_bytes);
    for fragment in &prefix.system_developer {
        if !matches!(fragment.role.as_str(), "system" | "developer") || system_budget == 0 {
            continue;
        }
        let text = normalize_identity_text(&fragment.text);
        let field = bounded_identity_field(&fragment.role, &text, system_budget);
        if field.is_empty() {
            continue;
        }
        remaining = remaining.saturating_sub(field.len());
        system_budget = system_budget.saturating_sub(field.len());
        digest.update(field);
    }
    let user_field = bounded_identity_field("user", &user_text, remaining);
    if !user_field.is_empty() {
        digest.update(user_field);
    }
    Some(SessionIdentity {
        digest: digest.finalize().into(),
        source: SessionSource::AutomaticSession,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn explicit_and_automatic_identities_are_hashed_and_surface_scoped() {
        let identity = session_identity_from_header(Some("fixture-session")).expect("identity");
        assert_eq!(identity.source, SessionSource::ExplicitSession);
        assert_eq!(
            identity.digest,
            [
                0xd6, 0x44, 0x09, 0x83, 0xc4, 0x54, 0xc2, 0xe5, 0x99, 0x9f, 0xdb, 0x66, 0xbb, 0xe9,
                0xcf, 0x5f, 0x89, 0xa8, 0xf5, 0x84, 0x7d, 0x6c, 0x95, 0x78, 0xef, 0x14, 0xaf, 0xd2,
                0x6e, 0xee, 0x12, 0x2c,
            ]
        );
        assert!(session_identity_from_header(None).is_none());
        assert!(session_identity_from_header(Some("bad\nvalue")).is_none());
        assert!(session_identity_from_header(Some(&"x".repeat(513))).is_none());
        assert!(!format!("{identity:?}").contains("fixture-session"));
        let prefix = ConversationPrefix::new(
            vec![ConversationTextFragment::new(
                "system",
                "stable instruction",
            )],
            Some("first question".into()),
        );
        let automatic = automatic_session_identity(&prefix, "chat_completions").expect("automatic");
        assert_eq!(automatic.source, SessionSource::AutomaticSession);
        assert!(automatic_session_identity(&prefix, "responses").is_none());
        assert!(!format!("{automatic:?}").contains("first question"));
        let long_system = "shared system prefix ".repeat(2_000);
        let first = automatic_session_identity(
            &ConversationPrefix::new(
                vec![ConversationTextFragment::new("system", long_system.clone())],
                Some("first request".into()),
            ),
            "chat_completions",
        );
        let second = automatic_session_identity(
            &ConversationPrefix::new(
                vec![ConversationTextFragment::new("system", long_system)],
                Some("second request".into()),
            ),
            "chat_completions",
        );
        assert_ne!(first, second);
    }
}
