//! Credential-verification policy.
//!
//! Model discovery and credential verification are different facts. A
//! provider may publish its catalog without a credential, a runtime may carry
//! a static catalog, and a documented `/models` endpoint may simply answer
//! `200` to anyone. None of those prove that a credential is accepted.
//!
//! This module keeps the two apart. [`CredentialProof`] states what a runtime
//! is *allowed* to conclude about a credential, [`CatalogEvidence`] states what
//! a discovery request actually returned, and [`CredentialStatus`] is the only
//! conclusion [`ProviderVerificationPolicy::assess`] will produce. The crate
//! never executes a request, so it never upgrades a credential on its own.

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// What a runtime is permitted to conclude about a provider credential
/// without running inference.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CredentialProof {
    /// The profile requires no credential, so there is nothing to prove.
    NotRequired,
    /// A first-party-documented, authenticated, non-billable metadata endpoint
    /// can prove the credential. Only an *authenticated* success counts; see
    /// [`ProviderVerificationPolicy::assess`].
    AuthenticatedMetadata {
        /// Absolute path of the authenticated metadata endpoint.
        path: String,
    },
    /// The credential cannot be proven safely at connect time. It stays
    /// unverified until real inference demonstrates acceptance.
    DeferredUntilInference,
    /// A probe may prove the credential only when the operator separately
    /// configures and authorizes it.
    ExplicitProbeRequired,
}

impl CredentialProof {
    /// True when a runtime may prove a credential from metadata alone.
    ///
    /// `DeferredUntilInference` and `ExplicitProbeRequired` deliberately answer
    /// `false`: both require an action (inference, or an authorized probe) that
    /// the profile metadata cannot stand in for.
    pub fn proves_credential_without_inference(&self) -> bool {
        matches!(self, Self::AuthenticatedMetadata { .. })
    }
}

/// What a model-catalog (discovery) request actually returned.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CatalogEvidence {
    /// The catalog was reachable without any credential — a public endpoint, a
    /// local model list, or a bundled static list. It proves availability only.
    PublicStatic,
    /// The catalog was returned *by* the provider credential.
    Authenticated,
    /// Discovery did not succeed.
    Unavailable,
}

/// The only credential conclusion the contract permits.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CredentialStatus {
    /// No credential is required, so there is nothing to verify.
    NotApplicable,
    /// The provider credential is proven by authenticated evidence.
    Verified,
    /// Nothing proves the credential yet. This is the expected resting state
    /// for every credentialed profile that is not evidence-qualified for
    /// metadata verification.
    Unverified,
}

/// Provider verification metadata: runtime probe hints plus the policy that
/// decides what those hints are allowed to conclude.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ProviderVerificationPolicy {
    /// Legacy probe hint preserved verbatim from the bundled profile data.
    /// It selects which request a runtime *may* make; it never asserts an
    /// outcome.
    pub probe_model: Option<String>,
    /// Legacy protocol hint for [`Self::probe_model`].
    pub probe_protocol: Option<String>,
    /// Whether catalog discovery is required before the provider is usable.
    pub require_models: bool,
    /// What a runtime may conclude about the credential without inference.
    pub credential_proof: CredentialProof,
}

impl Default for ProviderVerificationPolicy {
    fn default() -> Self {
        Self {
            probe_model: None,
            probe_protocol: None,
            require_models: true,
            credential_proof: CredentialProof::DeferredUntilInference,
        }
    }
}

impl ProviderVerificationPolicy {
    /// Conservative default for credentialed providers.
    ///
    /// A profile only leaves the deferred state through
    /// [`CredentialProof::AuthenticatedMetadata`], which requires first-party
    /// evidence that a specific metadata endpoint demands the credential. No
    /// bundled profile is evidence-qualified for that today, so no bundled
    /// profile guesses its way out of `Unverified`.
    pub fn conservative() -> Self {
        Self::default()
    }

    /// Policy for a profile that requires no credential.
    pub fn not_required() -> Self {
        Self {
            credential_proof: CredentialProof::NotRequired,
            ..Self::default()
        }
    }

    /// Policy for a credentialed profile proven by an authenticated metadata
    /// endpoint.
    pub fn authenticated_metadata(path: impl Into<String>) -> Self {
        Self {
            credential_proof: CredentialProof::AuthenticatedMetadata { path: path.into() },
            ..Self::default()
        }
    }

    /// Whether a runtime may prove this provider's credential from metadata
    /// alone.
    pub fn proves_credential_without_inference(&self) -> bool {
        self.credential_proof.proves_credential_without_inference()
    }

    /// Credential status implied by the policy alone, before any request.
    ///
    /// A credentialed profile is never reported verified here: the policy is a
    /// statement about what *may* be proven, not a record of what happened.
    pub fn status_before_any_request(&self) -> CredentialStatus {
        match self.credential_proof {
            CredentialProof::NotRequired => CredentialStatus::NotApplicable,
            _ => CredentialStatus::Unverified,
        }
    }

    /// Credential status after observing `catalog`.
    ///
    /// Public and unavailable catalog evidence can never verify a credential,
    /// even for a profile that names an authenticated metadata endpoint.
    pub fn assess(&self, catalog: CatalogEvidence) -> CredentialStatus {
        match self.credential_proof {
            CredentialProof::NotRequired => CredentialStatus::NotApplicable,
            CredentialProof::AuthenticatedMetadata { .. } => match catalog {
                CatalogEvidence::Authenticated => CredentialStatus::Verified,
                CatalogEvidence::PublicStatic | CatalogEvidence::Unavailable => {
                    CredentialStatus::Unverified
                }
            },
            CredentialProof::DeferredUntilInference | CredentialProof::ExplicitProbeRequired => {
                CredentialStatus::Unverified
            }
        }
    }

    /// Whether catalog discovery alone would establish a usable credential.
    ///
    /// This is the drift the milestone exists to prevent: treating a reachable
    /// model list as proof that a key is accepted.
    pub fn catalog_implies_credential(&self, catalog: CatalogEvidence) -> bool {
        self.assess(catalog) == CredentialStatus::Verified
    }
}

/// Declared first-party expectation for a provider's model catalog.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CatalogAuthentication {
    /// The metadata endpoint answers without a credential.
    Public,
    /// The metadata endpoint requires the provider credential.
    RequiresCredential,
    /// Not evidence-qualified.
    Unqualified,
}

impl CatalogAuthentication {
    /// Map a declared expectation plus an observed response into evidence.
    ///
    /// A catalog obtained without a credential is [`CatalogEvidence::PublicStatic`]
    /// regardless of what the profile expected; expectation never upgrades
    /// observation.
    pub fn observed(&self, authenticated: bool) -> CatalogEvidence {
        if authenticated {
            CatalogEvidence::Authenticated
        } else {
            CatalogEvidence::PublicStatic
        }
    }
}

/// Profile-level verification facts shared by the provider-profile parser and
/// consumer-facing verification policy.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields, default)]
pub struct ProfileVerification {
    /// Legacy probe hint preserved verbatim from bundled profile data.
    pub probe_model: Option<String>,
    /// Legacy protocol hint for [`Self::probe_model`].
    pub probe_protocol: Option<String>,
    /// Whether catalog discovery is required before the provider is usable.
    pub require_models: bool,
    /// Declared first-party statement about whether the catalog endpoint needs
    /// the provider credential. `None` means unproven either way.
    pub models_require_authentication: Option<bool>,
}

impl Default for ProfileVerification {
    fn default() -> Self {
        Self {
            probe_model: None,
            probe_protocol: None,
            require_models: true,
            models_require_authentication: None,
        }
    }
}

impl ProfileVerification {
    /// Declared first-party expectation for this profile's model catalog.
    ///
    /// `None` means unproven either way, which is deliberately *not* the same
    /// as "public".
    pub fn catalog_authentication(&self) -> CatalogAuthentication {
        match self.models_require_authentication {
            None => CatalogAuthentication::Unqualified,
            Some(true) => CatalogAuthentication::RequiresCredential,
            Some(false) => CatalogAuthentication::Public,
        }
    }

    /// Build the consumer-facing policy for a profile whose auth mode is
    /// `none`.
    pub fn policy_without_credential(&self) -> ProviderVerificationPolicy {
        ProviderVerificationPolicy {
            probe_model: self.probe_model.clone(),
            probe_protocol: self.probe_protocol.clone(),
            require_models: self.require_models,
            credential_proof: CredentialProof::NotRequired,
        }
    }

    /// Build the conservative policy for a credentialed profile.
    ///
    /// `authenticated_metadata` must be supplied only from first-party
    /// evidence; [`None`] keeps the credential unverified until real inference.
    pub fn policy_with_credential(
        &self,
        authenticated_metadata: Option<&str>,
    ) -> ProviderVerificationPolicy {
        let credential_proof = match authenticated_metadata {
            Some(path) => CredentialProof::AuthenticatedMetadata {
                path: path.to_owned(),
            },
            None => CredentialProof::DeferredUntilInference,
        };
        ProviderVerificationPolicy {
            probe_model: self.probe_model.clone(),
            probe_protocol: self.probe_protocol.clone(),
            require_models: self.require_models,
            credential_proof,
        }
    }
}

/// Rejected verification metadata.
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum VerificationError {
    #[error("verification probe_protocol must be openai or anthropic")]
    ProbeProtocol,
    #[error("verification probe_model must not be empty")]
    ProbeModel,
}
