//! Sibling-consumer fixture: the smallest crate a downstream project needs to
//! resolve provider facts without any EggPool runtime, configuration, or
//! daemon code.
//!
//! It compiles with a single path dependency on `eggpool-provider-profile` and
//! performs the work a consumer integration actually does: parse the canonical
//! bundled profiles, resolve a provider's endpoint and per-surface
//! authentication, read the credential-verification policy, and look up an
//! exact model-to-wire hint.

use eggpool_provider_profile::{
    CredentialStatus, ProviderProfileRegistry, WireSurface, compose_url,
};

/// Resolved connection facts for one provider model id, as a consumer would
/// need them.
pub struct ResolvedDispatch<'a> {
    pub base_url: &'a str,
    pub surface_url: String,
    pub auth_header: &'a str,
    pub auth_mode: &'static str,
    pub credential_status: CredentialStatus,
}

/// Parse the canonical bundled profiles without any runtime dependency.
pub fn bundled_profiles() -> ProviderProfileRegistry {
    ProviderProfileRegistry::embedded().expect("embedded provider profiles parse and validate")
}

/// Resolve the dispatch facts a consumer needs for one exact model id.
///
/// Returns `None` when the provider or the model id has no reviewed hint; the
/// lookup never guesses.
pub fn resolve_dispatch<'a>(
    registry: &'a ProviderProfileRegistry,
    provider_id: &str,
    model_id: &str,
) -> Option<ResolvedDispatch<'a>> {
    let profile = registry.get(provider_id)?;
    let preference = profile.model_wire_preference(model_id)?;
    let surface = preference.preferred_surface;
    let auth = profile.surface_auth(surface)?;
    Some(ResolvedDispatch {
        base_url: &profile.base_url,
        surface_url: profile.surface_url(surface)?,
        auth_header: &auth.header,
        auth_mode: auth.mode.as_str(),
        credential_status: profile.verification_policy().status_before_any_request(),
    })
}

/// Read the verification policy for a credentialed provider.
pub fn credential_status(
    registry: &ProviderProfileRegistry,
    provider_id: &str,
) -> Option<CredentialStatus> {
    let profile = registry.get(provider_id)?;
    Some(profile.verification_policy().status_before_any_request())
}

/// Independently confirm that every bundled provider resolves a base URL and at
/// least one wire surface endpoint.
pub fn dispatchable_providers() -> Vec<String> {
    bundled_profiles()
        .profiles()
        .filter(|profile| {
            WireSurface::ALL.iter().any(|surface| {
                profile.surface_auth(*surface).is_some()
                    && profile
                        .surface_url(*surface)
                        .is_some_and(|url| url.starts_with(&profile.base_url))
            })
        })
        .map(|profile| profile.id.clone())
        .collect()
}

/// Compose an endpoint the same way the contract documents it.
pub fn endpoint(base: &str, path: &str) -> String {
    compose_url(base, path)
}