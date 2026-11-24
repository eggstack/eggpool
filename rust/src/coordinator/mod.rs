//! Request coordinator boundaries.
//!
//! Durable publication follows local account selection. Provider dispatch,
//! wire negotiation, retries, and terminal finalization remain explicit
//! coordinator responsibilities with separate ownership boundaries.

mod attempt;
mod endpoints;
mod failure;
mod finalization;
mod finite;
mod publication;
mod reconciliation;
mod semantic;
mod streaming;
mod wire_resolver;

#[cfg(test)]
mod oauth_integration_tests;

pub use attempt::{
    AttemptBuilder, AttemptError, AttemptInput, AttemptPreparation, PreparedUpstreamAttempt,
    UpstreamResponseEvidence,
};
pub use failure::{
    EffectLedger, EffectLedgerError, FailureCategory, FailureDecisionEngine, FailureEffects,
    FailureObservation, FailureSource, NextAction, ProviderModelPresence, RetryPolicy, RetryScope,
    classify, is_wire_rejection_signal, parse_retry_after, retry_after_from_headers,
};
pub use finalization::{
    DurableFinalizer, FinalizationCommand, FinalizationData, FinalizationDrainError,
    FinalizationError, FinalizationHandle, FinalizationOutcome, FinalizationProgress,
    FinalizationResult, FinalizationSupervisor, SupervisorSnapshot, TerminalReference,
    TerminalReferenceOwner,
};
pub use finite::{
    ClientResponseHeaders, DownstreamResult, FiniteClientResponse, FiniteCoordinator,
    FiniteCoordinatorError, FiniteExecution, FiniteRequest, ResponseHandoffState,
    filter_response_headers, remote_compaction_capable,
};

pub use publication::{
    FinalizationIdentity, PostCommitInterruption, PublicationError, PublicationFaultInjector,
    PublicationInput, PublicationOutcome, PublicationService, PublicationStage, PublishedAttempt,
    RuntimePublicationReceipt, budget_proof_for_claim,
};

pub use reconciliation::{
    CoordinatorFaultInjector, CrashFaultPoint, CrashReconciler, DEFAULT_RECONCILIATION_BATCH_LIMIT,
    MAX_RECONCILIATION_BATCH_LIMIT, ReconciliationClassification, ReconciliationConfig,
    ReconciliationError, ReconciliationReport,
};

pub use streaming::{
    OUTCOME_CLIENT_CANCELLED, OUTCOME_COMPLETED_CANONICAL, OUTCOME_COMPLETED_COMPATIBILITY,
    OUTCOME_EMPTY_EOF, OUTCOME_FIRST_BYTE_TIMEOUT, OUTCOME_IDLE_TIMEOUT, OUTCOME_MALFORMED_EOF,
    OUTCOME_PREMATURE_EOF_BEFORE_BODY, OUTCOME_PREMATURE_EOF_MIDSTREAM,
    OUTCOME_RESPONSE_HEADER_TIMEOUT, OUTCOME_TERMINAL_FAILURE, OUTCOME_TERMINAL_INCOMPLETE,
    OUTCOME_UPSTREAM_MIDSTREAM_ERROR, StreamChunkError, StreamClientHeaders, StreamDiagnosticEvent,
    StreamDiagnostics, StreamDiagnosticsSnapshot, StreamPhase, StreamRequest, StreamTimeoutPolicy,
    StreamingCoordinator, StreamingCoordinatorError, StreamingExecution,
};

pub use endpoints::{
    EndpointError, EndpointExecution, InferenceOperation, InferenceOutcome, InferenceState,
    ResolvedInference, VirtualResolution, build_stream_response_headers, endpoint_error_body,
    endpoint_error_body_for, execute_compact_finite, execute_endpoint, execute_finite,
    execute_stream, new_proxy_request_id, parse_provider_qualified_model,
    validate_responses_stateless,
};
pub(crate) use endpoints::{
    build_inference_state_with_shared, build_inference_state_with_shared_and_accounts,
    compile_provider_profiles, execute_compact_finite_attributed, execute_embeddings_attributed,
    execute_endpoint_attributed,
};
pub(crate) use failure::terminalize_oauth_401;
pub(crate) use finalization::release_claim;

pub(crate) fn agent_401_for_account(
    observation: &FailureObservation,
    providers: &std::collections::BTreeMap<String, crate::config::ProviderConfig>,
) -> bool {
    observation.status == Some(401)
        && observation
            .provider_id
            .as_deref()
            .and_then(|provider_id| providers.get(provider_id))
            .and_then(|provider| {
                let account_name = observation.account_name.as_deref()?;
                provider
                    .accounts
                    .iter()
                    .find(|account| account.name == account_name)
            })
            .is_some_and(|account| account.auth_mode == crate::config::AccountAuthMode::Agent)
}

pub(crate) fn oauth_401_for_account(
    observation: &FailureObservation,
    providers: &std::collections::BTreeMap<String, crate::config::ProviderConfig>,
) -> bool {
    observation.status == Some(401)
        && observation
            .provider_id
            .as_deref()
            .and_then(|provider_id| providers.get(provider_id))
            .and_then(|provider| {
                let account_name = observation.account_name.as_deref()?;
                provider
                    .accounts
                    .iter()
                    .find(|account| account.name == account_name)
            })
            .is_some_and(|account| account.auth_mode == crate::config::AccountAuthMode::OAuth)
}

/// The effects that describe an owner-scoped credential-refresh retry.
///
/// The account is held constant and no account health evidence is applied:
/// the provider rejected a stale credential, not the account. The caller
/// applies these effects only after the refresh actually succeeded.
///
/// The published failure vocabulary does not move: `RetryScope` and
/// `NextAction` keep their public variants, so the library's public API is
/// unchanged. The recovery is described by its own scope and action *labels*,
/// which are the stable string contract, while the enums carry the control
/// flow: the retry stays on the same account and is admitted like any other
/// retry.
pub(crate) fn healed_credential_refresh_effects(effects: &FailureEffects) -> FailureEffects {
    let mut healed = effects.clone();
    healed.retry = true;
    healed.retry_scope = RetryScope::Wire;
    healed.action = NextAction::RetryWire;
    healed.retry_action = "refresh_credential_same_account".into();
    healed.retry_scope_label = "same_account_refreshed_credential".into();
    healed
}

/// Refresh an owner-bound OAuth credential that an upstream provider rejected.
///
/// The refresh is owner-scoped and compare-and-refresh: Eggpass returns the
/// newer stored token when another process already rotated the credential, so
/// a rotated refresh token is never spent twice. `None` means the request
/// terminalizes exactly as it would without a refresh, which keeps the
/// operator gate, the owner binding, and the account health evidence
/// unchanged.
pub(crate) async fn refresh_rejected_oauth_credential(
    manager: Option<&crate::oauth::OAuthAccountManager>,
    user_id: Option<&str>,
    provider_id: &str,
    account_name: &str,
    rejected_credential: Option<&crate::config::SecretString>,
) -> Option<crate::config::SecretString> {
    let manager = manager?;
    let user_id = user_id?;
    let rejected = rejected_credential?;
    match manager
        .refresh_rejected_credential_for_user(
            user_id,
            provider_id,
            account_name,
            rejected.expose_secret(),
        )
        .await
    {
        Ok(refreshed) => Some(refreshed),
        Err(error) => {
            tracing::warn!(
                provider = %provider_id,
                account = %account_name,
                error = ?error,
                "OAuth credential refresh after an upstream rejection failed"
            );
            None
        }
    }
}
pub use semantic::{
    ModelSelection, SELECTOR_MAX_RESPONSE_BYTES, SelectionSource, SelectorDiagnostics,
    SelectorFallback, SelectorPrompt, SemanticSelector, build_semantic_view, compile_repair_prompt,
    compile_selector_prompt, normalize_selector_text, parse_route_id, truncate_utf8,
};

pub use wire_resolver::{
    NegotiationLease, NegotiationResult, NegotiationRole, WireCandidate, WireResolution,
    WireResolver, WireResolverConfig, WireResolverConfigError, WireResolverPolicyStage,
    WireResolverSnapshot,
};

#[cfg(test)]
mod oauth_401_tests {
    use super::*;
    use crate::config::{AccountAuthMode, AccountConfig, ProviderConfig};
    use http::StatusCode;

    fn provider_with_account(auth_mode: AccountAuthMode) -> ProviderConfig {
        let mut provider = ProviderConfig::default();
        provider.accounts.push(AccountConfig {
            name: "account-a".to_owned(),
            auth_mode,
            ..AccountConfig::default()
        });
        provider
    }

    #[test]
    fn oauth_401_is_terminal_but_static_key_failure_policy_is_unchanged() {
        let mut oauth_provider = std::collections::BTreeMap::new();
        oauth_provider.insert(
            "xai".to_owned(),
            provider_with_account(AccountAuthMode::OAuth),
        );
        let mut static_provider = std::collections::BTreeMap::new();
        static_provider.insert(
            "xai".to_owned(),
            provider_with_account(AccountAuthMode::StaticKey),
        );
        let mut observation = FailureObservation::response(1, 1, StatusCode::UNAUTHORIZED);
        observation.provider_id = Some("xai".to_owned());
        observation.account_name = Some("account-a".to_owned());
        observation.credential_configured = true;

        assert!(oauth_401_for_account(&observation, &oauth_provider));
        assert!(!oauth_401_for_account(&observation, &static_provider));

        let (mut oauth_effects, _) = FailureDecisionEngine::new(RetryPolicy::default())
            .decide(&observation)
            .expect("401 classification succeeds");
        assert!(
            oauth_effects.retry,
            "baseline 401 policy may select another static account"
        );
        terminalize_oauth_401(&mut oauth_effects);
        assert!(!oauth_effects.retry);
        assert_eq!(oauth_effects.action, NextAction::Complete);
        assert_eq!(oauth_effects.retry_scope, RetryScope::None);

        let (static_effects, _) = FailureDecisionEngine::new(RetryPolicy::default())
            .decide(&observation)
            .expect("static 401 classification succeeds");
        assert!(static_effects.retry, "static-key policy remains unchanged");
    }
}
