//! Protocol-only stream conformance vectors (sans-I/O).
//!
//! This module publishes deterministic byte-level fixtures for every
//! [`StreamAdapterKind`](crate::codec::StreamAdapterKind) dialect plus the
//! helpers tests use to replay them at arbitrary transport chunk splits.
//! Vectors are byte slices only: no sockets, clocks, async runtimes, or
//! provider clients are involved.
//!
//! # Bounded observation contract
//!
//! Byte forwarding stays caller-owned: the kernel never buffers a complete
//! stream. [`StreamEventDecoder`](crate::stream::StreamEventDecoder) observes
//! bounded terminal/usage facts while returning source bytes to the caller
//! (`push` for translated decoding,
//! [`observe_native_push`](crate::stream::StreamEventDecoder::observe_native_push)
//! for native forwarding without materializing canonical batches). See
//! [`NativeStreamObservation`](crate::stream::NativeStreamObservation) for the
//! native-observation half of that contract.
//!
//! # Terminal distinguishability
//!
//! Every terminal case in [`stream_conformance_vectors`] is observable:
//! success, incomplete, provider-error, and malformed outcomes differ in
//! [`StreamTerminalOutcome`](crate::stream::StreamTerminalOutcome); EOF
//! before any payload differs from EOF after a partial body; and
//! post-terminal data keeps its success outcome while incrementing
//! `parser_error_count`.

use crate::codec::StreamAdapterKind;
use crate::stream::StreamTerminalOutcome;

/// One protocol-only stream fixture: raw SSE bytes plus the expected
/// terminal outcome after framing, decoding, and EOF finalization.
///
/// `expect_parser_errors` marks vectors (post-terminal data, malformed
/// frames) whose summary must carry a non-zero `parser_error_count` in
/// addition to `expected`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct StreamConformanceVector {
    pub name: &'static str,
    pub adapter: StreamAdapterKind,
    pub bytes: &'static [u8],
    pub expected: StreamTerminalOutcome,
    pub expect_parser_errors: bool,
}

/// Deterministic chunk-split points for a byte string of length `len`.
///
/// Returns the sorted, deduplicated boundary set `{0, 1, len/2, len-1, len}`
/// clipped to `[0, len]`. Tests add UTF-8-interior splits for fixtures
/// containing multi-byte characters (see the chunk-matrix tests below).
pub fn sse_split_points(len: usize) -> Vec<usize> {
    let mut points = vec![0, 1, len / 2, len.saturating_sub(1), len];
    points.sort_unstable();
    points.dedup();
    points.into_iter().filter(|point| *point <= len).collect()
}

/// Protocol-only conformance corpus: terminal evidence, usage completion,
/// malformed frames, post-terminal data, and incomplete final frames for
/// every streaming dialect.
///
/// Fixture text uses the fixed token `"hello"` plus the multi-byte marker
/// `"h\u{e9}llo \u{1f30d}"` so chunk-matrix tests can split inside a UTF-8
/// sequence deterministically. No fixture carries prompts, credentials, or
/// provider secrets.
pub fn stream_conformance_vectors() -> Vec<StreamConformanceVector> {
    use StreamAdapterKind as Kind;
    use StreamTerminalOutcome as Outcome;
    vec![
        // ---- OpenAI Chat SSE ----
        StreamConformanceVector {
            name: "chat_success",
            adapter: Kind::OpenaiChatSse,
            bytes: concat!(
                "data: {\"id\":\"chatcmpl-1\",\"object\":\"chat.completion.chunk\",\"model\":\"m\",",
                "\"choices\":[{\"index\":0,\"delta\":{\"content\":\"h\u{e9}llo \u{1f30d}\"},\"finish_reason\":null}]}\n\n",
                "data: {\"id\":\"chatcmpl-1\",\"object\":\"chat.completion.chunk\",\"model\":\"m\",",
                "\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n",
                "data: {\"usage\":{\"prompt_tokens\":2,\"completion_tokens\":3,\"total_tokens\":5}}\n\n",
                "data: [DONE]\n\n",
            )
            .as_bytes(),
            expected: Outcome::Success,
            expect_parser_errors: false,
        },
        StreamConformanceVector {
            name: "chat_provider_error",
            adapter: Kind::OpenaiChatSse,
            bytes: concat!(
                "event: error\n",
                "data: {\"error\":{\"type\":\"overloaded\",\"message\":\"busy\"}}\n\n",
            )
            .as_bytes(),
            expected: Outcome::ProviderError,
            expect_parser_errors: false,
        },
        StreamConformanceVector {
            name: "chat_malformed",
            adapter: Kind::OpenaiChatSse,
            bytes: b"data: {{{not json\n\n",
            expected: Outcome::Malformed,
            expect_parser_errors: true,
        },
        StreamConformanceVector {
            name: "chat_eof_before_body",
            adapter: Kind::OpenaiChatSse,
            bytes: b"",
            expected: Outcome::EofBeforeBody,
            expect_parser_errors: false,
        },
        StreamConformanceVector {
            name: "chat_eof_after_partial",
            adapter: Kind::OpenaiChatSse,
            bytes: concat!(
                "data: {\"id\":\"chatcmpl-1\",\"object\":\"chat.completion.chunk\",\"model\":\"m\",",
                "\"choices\":[{\"index\":0,\"delta\":{\"content\":\"partial\"},\"finish_reason\":null}]}\n\n",
            )
            .as_bytes(),
            expected: Outcome::EofAfterPartialBody,
            expect_parser_errors: false,
        },
        StreamConformanceVector {
            name: "chat_post_terminal_data",
            adapter: Kind::OpenaiChatSse,
            bytes: concat!(
                "data: {\"id\":\"chatcmpl-1\",\"object\":\"chat.completion.chunk\",\"model\":\"m\",",
                "\"choices\":[{\"index\":0,\"delta\":{},\"finish_reason\":\"stop\"}]}\n\n",
                "data: [DONE]\n\n",
                "data: {\"id\":\"chatcmpl-1\",\"object\":\"chat.completion.chunk\",\"model\":\"m\",",
                "\"choices\":[{\"index\":0,\"delta\":{\"content\":\"late\"},\"finish_reason\":null}]}\n\n",
            )
            .as_bytes(),
            expected: Outcome::Success,
            expect_parser_errors: true,
        },
        // ---- OpenAI Responses SSE ----
        StreamConformanceVector {
            name: "responses_success",
            adapter: Kind::OpenaiResponsesSse,
            bytes: concat!(
                "event: response.output_text.delta\n",
                "data: {\"type\":\"response.output_text.delta\",\"delta\":\"h\u{e9}llo \u{1f30d}\"}\n\n",
                "event: response.completed\n",
                "data: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp-1\",\"status\":\"completed\",",
                "\"usage\":{\"input_tokens\":2,\"output_tokens\":3,\"total_tokens\":5}}}\n\n",
            )
            .as_bytes(),
            expected: Outcome::Success,
            expect_parser_errors: false,
        },
        StreamConformanceVector {
            name: "responses_incomplete",
            adapter: Kind::OpenaiResponsesSse,
            bytes: concat!(
                "event: response.output_text.delta\n",
                "data: {\"type\":\"response.output_text.delta\",\"delta\":\"partial\"}\n\n",
                "event: response.incomplete\n",
                "data: {\"type\":\"response.incomplete\",\"response\":{\"id\":\"resp-1\",\"status\":\"incomplete\"}}\n\n",
            )
            .as_bytes(),
            expected: Outcome::Incomplete,
            expect_parser_errors: false,
        },
        StreamConformanceVector {
            name: "responses_provider_error",
            adapter: Kind::OpenaiResponsesSse,
            bytes: concat!(
                "event: error\n",
                "data: {\"type\":\"error\",\"error\":{\"type\":\"failed\",\"message\":\"boom\"}}\n\n",
            )
            .as_bytes(),
            expected: Outcome::ProviderError,
            expect_parser_errors: false,
        },
        StreamConformanceVector {
            name: "responses_malformed",
            adapter: Kind::OpenaiResponsesSse,
            bytes: b"event: response.completed\ndata: {{{not json\n\n",
            expected: Outcome::Malformed,
            expect_parser_errors: true,
        },
        StreamConformanceVector {
            name: "responses_eof_before_body",
            adapter: Kind::OpenaiResponsesSse,
            bytes: b"",
            expected: Outcome::EofBeforeBody,
            expect_parser_errors: false,
        },
        StreamConformanceVector {
            name: "responses_eof_after_partial",
            adapter: Kind::OpenaiResponsesSse,
            bytes: concat!(
                "event: response.output_text.delta\n",
                "data: {\"type\":\"response.output_text.delta\",\"delta\":\"partial\"}\n\n",
            )
            .as_bytes(),
            expected: Outcome::EofAfterPartialBody,
            expect_parser_errors: false,
        },
        StreamConformanceVector {
            name: "responses_post_terminal_data",
            adapter: Kind::OpenaiResponsesSse,
            bytes: concat!(
                "event: response.completed\n",
                "data: {\"type\":\"response.completed\",\"response\":{\"id\":\"resp-1\",\"status\":\"completed\"}}\n\n",
                "event: response.output_text.delta\n",
                "data: {\"type\":\"response.output_text.delta\",\"delta\":\"late\"}\n\n",
            )
            .as_bytes(),
            expected: Outcome::Success,
            expect_parser_errors: true,
        },
        // ---- Anthropic Messages SSE ----
        StreamConformanceVector {
            name: "anthropic_success",
            adapter: Kind::AnthropicMessagesSse,
            bytes: concat!(
                "event: message_start\n",
                "data: {\"type\":\"message_start\",\"message\":{\"id\":\"msg-1\",\"model\":\"m\"}}\n\n",
                "event: content_block_delta\n",
                "data: {\"type\":\"content_block_delta\",\"index\":0,",
                "\"delta\":{\"type\":\"text_delta\",\"text\":\"h\u{e9}llo \u{1f30d}\"}}\n\n",
                "event: message_delta\n",
                "data: {\"type\":\"message_delta\",\"delta\":{\"stop_reason\":\"end_turn\"},",
                "\"usage\":{\"input_tokens\":2,\"output_tokens\":3}}\n\n",
                "event: message_stop\n",
                "data: {\"type\":\"message_stop\"}\n\n",
            )
            .as_bytes(),
            expected: Outcome::Success,
            expect_parser_errors: false,
        },
        StreamConformanceVector {
            name: "anthropic_provider_error",
            adapter: Kind::AnthropicMessagesSse,
            bytes: concat!(
                "event: error\n",
                "data: {\"type\":\"error\",\"error\":{\"type\":\"overloaded\",\"message\":\"busy\"}}\n\n",
            )
            .as_bytes(),
            expected: Outcome::ProviderError,
            expect_parser_errors: false,
        },
        StreamConformanceVector {
            name: "anthropic_malformed",
            adapter: Kind::AnthropicMessagesSse,
            bytes: b"event: message_start\ndata: {{{not json\n\n",
            expected: Outcome::Malformed,
            expect_parser_errors: true,
        },
        StreamConformanceVector {
            name: "anthropic_eof_before_body",
            adapter: Kind::AnthropicMessagesSse,
            bytes: b"",
            expected: Outcome::EofBeforeBody,
            expect_parser_errors: false,
        },
        StreamConformanceVector {
            name: "anthropic_eof_after_partial",
            adapter: Kind::AnthropicMessagesSse,
            bytes: concat!(
                "event: content_block_delta\n",
                "data: {\"type\":\"content_block_delta\",\"index\":0,",
                "\"delta\":{\"type\":\"text_delta\",\"text\":\"partial\"}}\n\n",
            )
            .as_bytes(),
            expected: Outcome::EofAfterPartialBody,
            expect_parser_errors: false,
        },
        StreamConformanceVector {
            name: "anthropic_post_terminal_data",
            adapter: Kind::AnthropicMessagesSse,
            bytes: concat!(
                "event: message_stop\n",
                "data: {\"type\":\"message_stop\"}\n\n",
                "event: content_block_delta\n",
                "data: {\"type\":\"content_block_delta\",\"index\":0,",
                "\"delta\":{\"type\":\"text_delta\",\"text\":\"late\"}}\n\n",
            )
            .as_bytes(),
            expected: Outcome::Success,
            expect_parser_errors: true,
        },
        // ---- Gemini Interactions SSE ----
        StreamConformanceVector {
            name: "interactions_success",
            adapter: Kind::GeminiInteractionsSse,
            bytes: concat!(
                "event: interaction.created\n",
                "data: {\"event_type\":\"interaction.created\",\"interaction\":{\"id\":\"in-1\",\"model\":\"m\"}}\n\n",
                "event: step.delta\n",
                "data: {\"event_type\":\"step.delta\",\"index\":0,",
                "\"delta\":{\"type\":\"text\",\"text\":\"h\u{e9}llo \u{1f30d}\"}}\n\n",
                "event: interaction.completed\n",
                "data: {\"event_type\":\"interaction.completed\",\"interaction\":{\"id\":\"in-1\",\"status\":\"completed\",",
                "\"usage\":{\"total_input_tokens\":2,\"total_output_tokens\":3,\"total_tokens\":5}}}\n\n",
            )
            .as_bytes(),
            expected: Outcome::Success,
            expect_parser_errors: false,
        },
        StreamConformanceVector {
            name: "interactions_incomplete",
            adapter: Kind::GeminiInteractionsSse,
            bytes: concat!(
                "event: interaction.completed\n",
                "data: {\"event_type\":\"interaction.completed\",",
                "\"interaction\":{\"id\":\"in-1\",\"status\":\"truncated\"}}\n\n",
            )
            .as_bytes(),
            expected: Outcome::Incomplete,
            expect_parser_errors: false,
        },
        StreamConformanceVector {
            name: "interactions_provider_error",
            adapter: Kind::GeminiInteractionsSse,
            bytes: concat!(
                "event: error\n",
                "data: {\"event_type\":\"error\",\"error\":{\"type\":\"failed\",\"message\":\"boom\"}}\n\n",
            )
            .as_bytes(),
            expected: Outcome::ProviderError,
            expect_parser_errors: false,
        },
        StreamConformanceVector {
            name: "interactions_malformed",
            adapter: Kind::GeminiInteractionsSse,
            bytes: b"event: step.delta\ndata: {{{not json\n\n",
            expected: Outcome::Malformed,
            expect_parser_errors: true,
        },
        StreamConformanceVector {
            name: "interactions_eof_before_body",
            adapter: Kind::GeminiInteractionsSse,
            bytes: b"",
            expected: Outcome::EofBeforeBody,
            expect_parser_errors: false,
        },
        StreamConformanceVector {
            name: "interactions_eof_after_partial",
            adapter: Kind::GeminiInteractionsSse,
            bytes: concat!(
                "event: step.delta\n",
                "data: {\"event_type\":\"step.delta\",\"index\":0,",
                "\"delta\":{\"type\":\"text\",\"text\":\"partial\"}}\n\n",
            )
            .as_bytes(),
            expected: Outcome::EofAfterPartialBody,
            expect_parser_errors: false,
        },
        StreamConformanceVector {
            name: "interactions_post_terminal_data",
            adapter: Kind::GeminiInteractionsSse,
            bytes: concat!(
                "event: interaction.completed\n",
                "data: {\"event_type\":\"interaction.completed\",",
                "\"interaction\":{\"id\":\"in-1\",\"status\":\"completed\"}}\n\n",
                "event: step.delta\n",
                "data: {\"event_type\":\"step.delta\",\"index\":0,",
                "\"delta\":{\"type\":\"text\",\"text\":\"late\"}}\n\n",
            )
            .as_bytes(),
            expected: Outcome::Success,
            expect_parser_errors: true,
        },
        // ---- Gemini GenerateContent SSE (data-only frames) ----
        StreamConformanceVector {
            name: "generate_content_success",
            adapter: Kind::GeminiGenerateContentSse,
            bytes: concat!(
                "data: {\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"h\u{e9}llo \u{1f30d}\"}]}}]}\n\n",
                "data: {\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"!\"}]},\"finishReason\":\"STOP\"}],",
                "\"usageMetadata\":{\"total_input_tokens\":2,\"total_output_tokens\":3,\"total_tokens\":5}}\n\n",
            )
            .as_bytes(),
            expected: Outcome::Success,
            expect_parser_errors: false,
        },
        StreamConformanceVector {
            name: "generate_content_incomplete",
            adapter: Kind::GeminiGenerateContentSse,
            bytes: concat!(
                "data: {\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"partial\"}]},",
                "\"finishReason\":\"MAX_TOKENS\"}]}\n\n",
            )
            .as_bytes(),
            expected: Outcome::Incomplete,
            expect_parser_errors: false,
        },
        StreamConformanceVector {
            name: "generate_content_malformed",
            adapter: Kind::GeminiGenerateContentSse,
            bytes: b"data: {{{not json\n\n",
            expected: Outcome::Malformed,
            expect_parser_errors: true,
        },
        StreamConformanceVector {
            name: "generate_content_eof_before_body",
            adapter: Kind::GeminiGenerateContentSse,
            bytes: b"",
            expected: Outcome::EofBeforeBody,
            expect_parser_errors: false,
        },
        StreamConformanceVector {
            name: "generate_content_eof_after_partial",
            adapter: Kind::GeminiGenerateContentSse,
            bytes: "data: {\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"partial\"}]}}]}\n\n"
                .as_bytes(),
            expected: Outcome::EofAfterPartialBody,
            expect_parser_errors: false,
        },
        StreamConformanceVector {
            name: "generate_content_post_terminal_data",
            adapter: Kind::GeminiGenerateContentSse,
            bytes: concat!(
                "data: {\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"hi\"}]},\"finishReason\":\"STOP\"}]}\n\n",
                "data: {\"candidates\":[{\"content\":{\"parts\":[{\"text\":\"late\"}]}}]}\n\n",
            )
            .as_bytes(),
            expected: Outcome::Success,
            expect_parser_errors: true,
        },
    ]
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::stream::StreamEventDecoder;

    /// Interior byte offsets of the first multi-byte character in `bytes`.
    ///
    /// Finds the 4-byte `U+1F30D` marker every success vector carries and
    /// returns the three splits strictly inside its encoding, so the matrix
    /// covers transport splits mid-code-point.
    fn utf8_interior_splits(bytes: &[u8]) -> Vec<usize> {
        let marker = "🌍".as_bytes();
        let Some(start) = bytes
            .windows(marker.len())
            .position(|window| window == marker)
        else {
            return Vec::new();
        };
        (1..marker.len()).map(|offset| start + offset).collect()
    }

    fn split_points_for(bytes: &[u8]) -> Vec<usize> {
        let mut points = sse_split_points(bytes.len());
        points.extend(utf8_interior_splits(bytes));
        points.sort_unstable();
        points.dedup();
        points
    }

    fn decode_all(
        adapter: StreamAdapterKind,
        chunks: &[&[u8]],
    ) -> (
        Vec<crate::ir::CanonicalEvent>,
        crate::stream::StreamTerminalSummary,
    ) {
        let mut decoder = StreamEventDecoder::new(adapter);
        let mut events = Vec::new();
        for chunk in chunks {
            match decoder.push(chunk) {
                Ok(mut batch) => events.append(&mut batch),
                Err(_) => events.clear(),
            }
        }
        let (mut tail, summary) = decoder.finalize_events().expect("finalize");
        events.append(&mut tail);
        (events, summary)
    }

    #[test]
    fn sse_split_points_cover_boundaries() {
        assert_eq!(sse_split_points(0), vec![0]);
        assert_eq!(sse_split_points(1), vec![0, 1]);
        let points = sse_split_points(100);
        assert!(points.contains(&0));
        assert!(points.contains(&1));
        assert!(points.contains(&50));
        assert!(points.contains(&99));
        assert!(points.contains(&100));
        assert!(points.windows(2).all(|pair| pair[0] < pair[1]));
    }

    #[test]
    fn every_adapter_has_terminal_vectors() {
        let vectors = stream_conformance_vectors();
        for adapter in [
            StreamAdapterKind::OpenaiChatSse,
            StreamAdapterKind::OpenaiResponsesSse,
            StreamAdapterKind::AnthropicMessagesSse,
            StreamAdapterKind::GeminiInteractionsSse,
            StreamAdapterKind::GeminiGenerateContentSse,
        ] {
            let names: Vec<&str> = vectors
                .iter()
                .filter(|vector| vector.adapter == adapter)
                .map(|vector| vector.name)
                .collect();
            assert!(
                names.iter().any(|name| name.contains("success")),
                "{adapter:?} needs a success vector"
            );
            assert!(
                names.iter().any(|name| name.contains("malformed")),
                "{adapter:?} needs a malformed vector"
            );
            assert!(
                names.iter().any(|name| name.contains("eof_before")),
                "{adapter:?} needs an EOF-before-body vector"
            );
            assert!(
                names.iter().any(|name| name.contains("eof_after")),
                "{adapter:?} needs an EOF-after-partial vector"
            );
            assert!(
                names.iter().any(|name| name.contains("post_terminal")),
                "{adapter:?} needs a post-terminal vector"
            );
        }
    }

    #[test]
    fn conformance_vectors_produce_expected_terminal_outcomes() {
        for vector in stream_conformance_vectors() {
            let (events, summary) = decode_all(vector.adapter, &[vector.bytes]);
            assert_eq!(
                summary.outcome, vector.expected,
                "vector {} on {:?} (events: {events:?})",
                vector.name, vector.adapter
            );
            assert_eq!(
                summary.parser_error_count > 0,
                vector.expect_parser_errors,
                "vector {} on {:?}: parser_error_count={}",
                vector.name,
                vector.adapter,
                summary.parser_error_count
            );
            // Terminal evidence stays strict: success-family outcomes carry
            // terminal evidence; EOF outcomes carry none.
            match vector.expected {
                StreamTerminalOutcome::Success
                | StreamTerminalOutcome::Incomplete
                | StreamTerminalOutcome::ProviderError => {
                    assert!(
                        summary.saw_terminal_event,
                        "vector {} must observe terminal evidence",
                        vector.name
                    );
                }
                StreamTerminalOutcome::EofBeforeBody
                | StreamTerminalOutcome::EofAfterPartialBody => {
                    assert!(
                        !summary.saw_terminal_event,
                        "vector {} must not claim terminal evidence",
                        vector.name
                    );
                }
                StreamTerminalOutcome::Malformed => {}
            }
            // EOF-before-body observes no payload; every other non-empty
            // vector does.
            if vector.expected == StreamTerminalOutcome::EofBeforeBody {
                assert!(!summary.saw_payload, "vector {}", vector.name);
            } else if !vector.bytes.is_empty() {
                assert!(summary.saw_payload, "vector {}", vector.name);
            }
        }
    }

    #[test]
    fn terminal_cases_are_mutually_distinguishable() {
        // Collect one vector per outcome family and assert the decoder maps
        // each to a distinct observable (outcome, or parser-error count for
        // post-terminal data which shares Success).
        let vectors = stream_conformance_vectors();
        let find = |name: &str| {
            vectors
                .iter()
                .find(|vector| vector.name == name)
                .unwrap_or_else(|| panic!("vector {name} exists"))
        };
        let outcomes: Vec<StreamTerminalOutcome> = [
            "responses_success",
            "responses_incomplete",
            "responses_provider_error",
            "responses_malformed",
            "responses_eof_before_body",
            "responses_eof_after_partial",
        ]
        .iter()
        .map(|name| {
            decode_all(find(name).adapter, &[find(name).bytes])
                .1
                .outcome
        })
        .collect();
        assert_eq!(
            outcomes,
            vec![
                StreamTerminalOutcome::Success,
                StreamTerminalOutcome::Incomplete,
                StreamTerminalOutcome::ProviderError,
                StreamTerminalOutcome::Malformed,
                StreamTerminalOutcome::EofBeforeBody,
                StreamTerminalOutcome::EofAfterPartialBody,
            ]
        );
        let (_, post_terminal) = decode_all(
            find("responses_post_terminal_data").adapter,
            &[find("responses_post_terminal_data").bytes],
        );
        assert_eq!(post_terminal.outcome, StreamTerminalOutcome::Success);
        assert!(post_terminal.parser_error_count > 0);
    }

    #[test]
    fn chunk_split_matrix_is_deterministic_for_every_dialect() {
        for vector in stream_conformance_vectors() {
            let (reference_events, reference_summary) = decode_all(vector.adapter, &[vector.bytes]);
            // Success vectors must carry the multi-byte marker so the matrix
            // exercises UTF-8-interior splits on every dialect.
            if vector.name.contains("success") {
                assert!(
                    !utf8_interior_splits(vector.bytes).is_empty(),
                    "vector {} needs a multi-byte marker",
                    vector.name
                );
            }
            for point in split_points_for(vector.bytes) {
                let (head, tail) = vector.bytes.split_at(point);
                let (events, summary) = decode_all(vector.adapter, &[head, tail]);
                assert_eq!(
                    events, reference_events,
                    "vector {} split at {point}: events differ",
                    vector.name
                );
                assert_eq!(
                    summary, reference_summary,
                    "vector {} split at {point}: summary differs",
                    vector.name
                );
            }
        }
    }

    #[test]
    fn three_way_splits_agree_with_single_shot() {
        for vector in stream_conformance_vectors() {
            if vector.bytes.len() < 8 {
                continue;
            }
            let (reference_events, reference_summary) = decode_all(vector.adapter, &[vector.bytes]);
            let third = vector.bytes.len() / 3;
            let (events, summary) = decode_all(
                vector.adapter,
                &[
                    &vector.bytes[..third],
                    &vector.bytes[third..2 * third],
                    &vector.bytes[2 * third..],
                ],
            );
            assert_eq!(
                events, reference_events,
                "vector {}: three-way split events differ",
                vector.name
            );
            assert_eq!(
                summary, reference_summary,
                "vector {}: three-way split summary differs",
                vector.name
            );
        }
    }

    #[test]
    fn success_vectors_observe_complete_usage() {
        // Every success vector carries usage; the observer must fold it and
        // mark the stream usage-complete without buffering the stream.
        for name in [
            "chat_success",
            "responses_success",
            "anthropic_success",
            "interactions_success",
            "generate_content_success",
        ] {
            let vector = stream_conformance_vectors()
                .into_iter()
                .find(|vector| vector.name == name)
                .unwrap_or_else(|| panic!("vector {name} exists"));
            let (_, summary) = decode_all(vector.adapter, &[vector.bytes]);
            assert_eq!(summary.outcome, StreamTerminalOutcome::Success);
            assert!(summary.saw_usage_completion, "vector {name}");
            assert!(!summary.missing_final_usage, "vector {name}");
            assert!(summary.usage.is_some(), "vector {name}");
        }
    }

    #[test]
    fn native_observation_agrees_on_terminal_vectors() {
        // The native-observed path (caller-owned bytes, kernel-observed
        // facts) must reach the same terminal outcome as translated decoding
        // on the Responses terminal vectors, at every chunk split.
        use crate::stream::StreamEventDecoder;
        for vector in stream_conformance_vectors() {
            if vector.adapter != StreamAdapterKind::OpenaiResponsesSse {
                continue;
            }
            let mut reference = StreamEventDecoder::new(vector.adapter);
            let _ = reference.observe_native_push(vector.bytes);
            let reference_summary = reference.finalize_observed().expect("finalize");
            let (_, translated) = decode_all(vector.adapter, &[vector.bytes]);
            assert_eq!(
                reference_summary.outcome, translated.outcome,
                "vector {}: native vs translated outcome",
                vector.name
            );
            for point in split_points_for(vector.bytes) {
                let (head, tail) = vector.bytes.split_at(point);
                let mut decoder = StreamEventDecoder::new(vector.adapter);
                let _ = decoder.observe_native_push(head);
                let _ = decoder.observe_native_push(tail);
                let observed = decoder.finalize_observed().expect("finalize");
                assert_eq!(
                    observed, reference_summary,
                    "vector {} split at {point}: native observation differs",
                    vector.name
                );
            }
        }
    }

    #[test]
    fn package_has_no_runtime_or_network_dependencies() {
        let manifest = include_str!("../Cargo.toml");
        for forbidden in [
            "tokio", "axum", "hyper", "reqwest", "eggress", "eggfetch", "rusqlite", "sqlite",
        ] {
            assert!(
                !manifest.contains(forbidden),
                "eggpool-wire must not depend on {forbidden}"
            );
        }
        for required in ["serde", "serde_json", "sha2", "thiserror", "toml"] {
            assert!(
                manifest.contains(required),
                "eggpool-wire keeps its declared dependency on {required}"
            );
        }
    }
}
