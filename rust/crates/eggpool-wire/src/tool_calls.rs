//! Bounded reconstruction of complete tool calls from canonical stream events.

use std::collections::BTreeMap;

use thiserror::Error;

use crate::ir::{CanonicalEvent, CanonicalEventType};

pub const MAX_ACTIVE_CANONICAL_TOOL_CALLS: usize = 128;
pub const MAX_CANONICAL_TOOL_CALL_ARGUMENT_BYTES: usize = 1024 * 1024;
pub const MAX_CANONICAL_TOOL_CALL_TOTAL_BYTES: usize = 4 * 1024 * 1024;

#[derive(Clone, PartialEq, Eq, PartialOrd, Ord)]
pub enum ToolCallIdentity {
    CallId(String),
    SourceIndex(usize),
}

impl std::fmt::Debug for ToolCallIdentity {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::CallId(_) => formatter.write_str("CallId(<redacted>)"),
            Self::SourceIndex(index) => formatter.debug_tuple("SourceIndex").field(index).finish(),
        }
    }
}

#[derive(Clone, PartialEq, Eq)]
pub struct CompletedToolCall {
    pub identity: ToolCallIdentity,
    pub call_id: Option<String>,
    pub source_index: Option<usize>,
    pub name: String,
    pub arguments: String,
}

impl std::fmt::Debug for CompletedToolCall {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("CompletedToolCall")
            .field("identity", &self.identity)
            .field("has_call_id", &self.call_id.is_some())
            .field("source_index", &self.source_index)
            .field("name_bytes", &self.name.len())
            .field("argument_bytes", &self.arguments.len())
            .finish()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Error)]
pub enum ToolCallAccumulatorError {
    #[error("tool call has no stable identity")]
    MissingIdentity,
    #[error("tool call identity conflicts with an active call")]
    ConflictingIdentity,
    #[error("tool call name is missing")]
    MissingName,
    #[error("tool call resource limit exceeded")]
    ResourceLimit,
    #[error("provider stream ended with incomplete tool calls")]
    Incomplete,
    #[error("provider stream ended without terminal completion")]
    UnexpectedEof,
    #[error("provider stream terminated with an error")]
    ProviderError,
    #[error("provider stream reported incomplete response")]
    ResponseIncomplete,
    #[error("canonical events arrived after terminal completion")]
    PostTerminalData,
}

#[derive(Debug)]
struct ActiveCall {
    identity: ToolCallIdentity,
    call_id: Option<String>,
    source_index: Option<usize>,
    name: Option<String>,
    arguments: String,
}

/// Incremental, per-stream completed-tool-call accumulator.
#[derive(Default)]
pub struct CanonicalToolCallAccumulator {
    active: BTreeMap<ToolCallIdentity, ActiveCall>,
    index_to_identity: BTreeMap<usize, ToolCallIdentity>,
    retained_bytes: usize,
    terminal: bool,
}

impl std::fmt::Debug for CanonicalToolCallAccumulator {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("CanonicalToolCallAccumulator")
            .field("active_calls", &self.active.len())
            .field("retained_bytes", &self.retained_bytes)
            .field("terminal", &self.terminal)
            .finish()
    }
}

impl CanonicalToolCallAccumulator {
    #[must_use]
    pub fn new() -> Self {
        Self::default()
    }

    pub fn push(
        &mut self,
        event: &CanonicalEvent,
    ) -> Result<Vec<CompletedToolCall>, ToolCallAccumulatorError> {
        if self.terminal {
            return Err(ToolCallAccumulatorError::PostTerminalData);
        }
        let result = match event.event_type {
            CanonicalEventType::ToolCallStart => self.start(event),
            CanonicalEventType::ToolCallArgumentsDelta => self.append(event),
            CanonicalEventType::ToolCallStop => self.stop(event),
            CanonicalEventType::ContentStop => {
                if event.index.is_some() || event.call_id.is_some() {
                    self.stop(event)
                } else {
                    Ok(Vec::new())
                }
            }
            CanonicalEventType::ResponseComplete => {
                self.terminal = true;
                self.drain_all()
            }
            CanonicalEventType::ResponseIncomplete => {
                self.fail(ToolCallAccumulatorError::ResponseIncomplete)
            }
            CanonicalEventType::Error => self.fail(ToolCallAccumulatorError::ProviderError),
            _ => Ok(Vec::new()),
        };
        if result.is_err() {
            self.clear();
            self.terminal = true;
        }
        result
    }

    /// Mark transport EOF. EOF is never treated as successful completion.
    pub fn finish(&mut self) -> Result<Vec<CompletedToolCall>, ToolCallAccumulatorError> {
        if !self.terminal {
            self.clear();
            return Err(ToolCallAccumulatorError::UnexpectedEof);
        }
        if self.active.is_empty() {
            Ok(Vec::new())
        } else {
            self.clear();
            Err(ToolCallAccumulatorError::Incomplete)
        }
    }

    fn start(
        &mut self,
        event: &CanonicalEvent,
    ) -> Result<Vec<CompletedToolCall>, ToolCallAccumulatorError> {
        let identity = self.identity(event)?;
        if self.active.contains_key(&identity)
            || event.index.is_some_and(|index| {
                self.index_to_identity
                    .get(&index)
                    .is_some_and(|existing| existing != &identity)
            })
        {
            return Err(ToolCallAccumulatorError::ConflictingIdentity);
        }
        if self.active.len() >= MAX_ACTIVE_CANONICAL_TOOL_CALLS {
            return Err(ToolCallAccumulatorError::ResourceLimit);
        }
        let name = event.name.clone().filter(|name| !name.trim().is_empty());
        if name.is_none() {
            return Err(ToolCallAccumulatorError::MissingName);
        }
        if let Some(index) = event.index {
            self.index_to_identity.insert(index, identity.clone());
        }
        self.active.insert(
            identity.clone(),
            ActiveCall {
                identity,
                call_id: event.call_id.clone(),
                source_index: event.index,
                name,
                arguments: String::new(),
            },
        );
        Ok(Vec::new())
    }

    fn append(
        &mut self,
        event: &CanonicalEvent,
    ) -> Result<Vec<CompletedToolCall>, ToolCallAccumulatorError> {
        let identity = self.resolve(event)?;
        let delta = event.delta.as_deref().unwrap_or_default();
        let current = self
            .active
            .get(&identity)
            .ok_or(ToolCallAccumulatorError::MissingIdentity)?;
        let call_bytes = current.arguments.len().saturating_add(delta.len());
        if call_bytes > MAX_CANONICAL_TOOL_CALL_ARGUMENT_BYTES
            || self.retained_bytes.saturating_add(delta.len()) > MAX_CANONICAL_TOOL_CALL_TOTAL_BYTES
        {
            self.clear();
            return Err(ToolCallAccumulatorError::ResourceLimit);
        }
        self.active
            .get_mut(&identity)
            .expect("call was checked")
            .arguments
            .push_str(delta);
        self.retained_bytes += delta.len();
        Ok(Vec::new())
    }

    fn stop(
        &mut self,
        event: &CanonicalEvent,
    ) -> Result<Vec<CompletedToolCall>, ToolCallAccumulatorError> {
        let identity = self.resolve(event)?;
        if let Some(final_arguments) = event.arguments.as_deref() {
            let old_len = self
                .active
                .get(&identity)
                .expect("identity resolved")
                .arguments
                .len();
            let aggregate = self
                .retained_bytes
                .saturating_sub(old_len)
                .saturating_add(final_arguments.len());
            if final_arguments.len() > MAX_CANONICAL_TOOL_CALL_ARGUMENT_BYTES
                || aggregate > MAX_CANONICAL_TOOL_CALL_TOTAL_BYTES
            {
                self.clear();
                return Err(ToolCallAccumulatorError::ResourceLimit);
            }
            self.retained_bytes = aggregate;
            self.active
                .get_mut(&identity)
                .expect("identity resolved")
                .arguments = final_arguments.to_owned();
        }
        self.complete(&identity).map(|call| vec![call])
    }

    fn drain_all(&mut self) -> Result<Vec<CompletedToolCall>, ToolCallAccumulatorError> {
        let identities = self.active.keys().cloned().collect::<Vec<_>>();
        identities
            .iter()
            .map(|identity| self.complete(identity))
            .collect()
    }

    fn complete(
        &mut self,
        identity: &ToolCallIdentity,
    ) -> Result<CompletedToolCall, ToolCallAccumulatorError> {
        let call = self
            .active
            .remove(identity)
            .ok_or(ToolCallAccumulatorError::MissingIdentity)?;
        let name = call.name.ok_or(ToolCallAccumulatorError::MissingName)?;
        self.retained_bytes = self.retained_bytes.saturating_sub(call.arguments.len());
        if let Some(index) = call.source_index {
            self.index_to_identity.remove(&index);
        }
        Ok(CompletedToolCall {
            identity: call.identity,
            call_id: call.call_id,
            source_index: call.source_index,
            name,
            arguments: call.arguments,
        })
    }

    fn identity(
        &self,
        event: &CanonicalEvent,
    ) -> Result<ToolCallIdentity, ToolCallAccumulatorError> {
        if let Some(call_id) = event.call_id.as_ref().filter(|value| !value.is_empty()) {
            return Ok(ToolCallIdentity::CallId(call_id.clone()));
        }
        event
            .index
            .map(ToolCallIdentity::SourceIndex)
            .ok_or(ToolCallAccumulatorError::MissingIdentity)
    }

    fn resolve(
        &self,
        event: &CanonicalEvent,
    ) -> Result<ToolCallIdentity, ToolCallAccumulatorError> {
        let proposed = self.identity(event)?;
        if let Some(index) = event.index
            && let Some(identity) = self.index_to_identity.get(&index)
        {
            if event.call_id.is_some() && &proposed != identity {
                return Err(ToolCallAccumulatorError::ConflictingIdentity);
            }
            return Ok(identity.clone());
        }
        if self.active.contains_key(&proposed) {
            return Ok(proposed);
        }
        Err(ToolCallAccumulatorError::MissingIdentity)
    }

    fn fail<T>(&mut self, error: ToolCallAccumulatorError) -> Result<T, ToolCallAccumulatorError> {
        self.clear();
        self.terminal = true;
        Err(error)
    }

    fn clear(&mut self) {
        self.active.clear();
        self.index_to_identity.clear();
        self.retained_bytes = 0;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ir::CanonicalUsage;

    fn event(
        event_type: CanonicalEventType,
        index: Option<usize>,
        call_id: Option<&str>,
        name: Option<&str>,
        delta: Option<&str>,
    ) -> CanonicalEvent {
        CanonicalEvent {
            event_type,
            response_id: None,
            model: None,
            index,
            delta: delta.map(str::to_owned),
            call_id: call_id.map(str::to_owned),
            name: name.map(str::to_owned),
            arguments: None,
            finish_reason: None,
            usage: Option::<CanonicalUsage>::None,
            error_type: None,
            error_message: None,
        }
    }

    #[test]
    fn interleaved_indexed_calls_complete_without_cross_contamination() {
        let mut accumulator = CanonicalToolCallAccumulator::new();
        for event in [
            event(
                CanonicalEventType::ToolCallStart,
                Some(0),
                Some("a"),
                Some("alpha"),
                None,
            ),
            event(
                CanonicalEventType::ToolCallStart,
                Some(1),
                Some("b"),
                Some("beta"),
                None,
            ),
            event(
                CanonicalEventType::ToolCallArgumentsDelta,
                Some(1),
                None,
                None,
                Some("B"),
            ),
            event(
                CanonicalEventType::ToolCallArgumentsDelta,
                Some(0),
                None,
                None,
                Some("A"),
            ),
        ] {
            assert!(accumulator.push(&event).expect("valid event").is_empty());
        }
        let completed = accumulator
            .push(&event(
                CanonicalEventType::ResponseComplete,
                None,
                None,
                None,
                None,
            ))
            .expect("terminal completes active calls");
        assert_eq!(completed.len(), 2);
        assert_eq!(completed[0].call_id.as_deref(), Some("a"));
        assert_eq!(completed[0].arguments, "A");
        assert_eq!(completed[1].call_id.as_deref(), Some("b"));
        assert_eq!(completed[1].arguments, "B");
        assert!(accumulator.finish().expect("clean terminal").is_empty());
        assert_eq!(
            accumulator.push(&event(CanonicalEventType::Usage, None, None, None, None)),
            Err(ToolCallAccumulatorError::PostTerminalData)
        );
    }

    #[test]
    fn stop_and_terminal_errors_are_authoritative_and_release_state() {
        let mut accumulator = CanonicalToolCallAccumulator::new();
        accumulator
            .push(&event(
                CanonicalEventType::ToolCallStart,
                Some(2),
                None,
                Some("lookup"),
                None,
            ))
            .unwrap();
        accumulator
            .push(&event(
                CanonicalEventType::ToolCallArgumentsDelta,
                Some(2),
                None,
                None,
                Some("{}"),
            ))
            .unwrap();
        let result = accumulator
            .push(&event(
                CanonicalEventType::ContentStop,
                Some(2),
                None,
                None,
                None,
            ))
            .unwrap();
        assert_eq!(result[0].identity, ToolCallIdentity::SourceIndex(2));
        assert_eq!(result[0].arguments, "{}");
        accumulator
            .push(&event(
                CanonicalEventType::ToolCallStart,
                Some(3),
                None,
                Some("pending"),
                None,
            ))
            .unwrap();
        assert_eq!(
            accumulator.push(&event(CanonicalEventType::Error, None, None, None, None)),
            Err(ToolCallAccumulatorError::ProviderError)
        );
        assert!(format!("{accumulator:?}").contains("active_calls: 0"));
    }

    #[test]
    fn malformed_identity_and_eof_fail_closed() {
        let mut accumulator = CanonicalToolCallAccumulator::new();
        assert_eq!(
            accumulator.push(&event(
                CanonicalEventType::ToolCallStart,
                None,
                None,
                Some("x"),
                None
            )),
            Err(ToolCallAccumulatorError::MissingIdentity)
        );
        let mut accumulator = CanonicalToolCallAccumulator::new();
        assert_eq!(
            accumulator.finish(),
            Err(ToolCallAccumulatorError::UnexpectedEof)
        );
    }

    #[test]
    fn duplicate_missing_and_oversized_calls_fail_and_clear_retained_state() {
        let mut accumulator = CanonicalToolCallAccumulator::new();
        let start = event(
            CanonicalEventType::ToolCallStart,
            Some(1),
            Some("id"),
            Some("tool"),
            None,
        );
        accumulator.push(&start).unwrap();
        assert_eq!(
            accumulator.push(&start),
            Err(ToolCallAccumulatorError::ConflictingIdentity)
        );
        assert!(format!("{accumulator:?}").contains("active_calls: 0"));

        let mut accumulator = CanonicalToolCallAccumulator::new();
        assert_eq!(
            accumulator.push(&event(
                CanonicalEventType::ToolCallStart,
                Some(1),
                None,
                None,
                None
            )),
            Err(ToolCallAccumulatorError::MissingName)
        );

        let mut accumulator = CanonicalToolCallAccumulator::new();
        accumulator
            .push(&event(
                CanonicalEventType::ToolCallStart,
                Some(1),
                None,
                Some("tool"),
                None,
            ))
            .unwrap();
        let over_limit = "x".repeat(MAX_CANONICAL_TOOL_CALL_ARGUMENT_BYTES + 1);
        assert_eq!(
            accumulator.push(&event(
                CanonicalEventType::ToolCallArgumentsDelta,
                Some(1),
                None,
                None,
                Some(&over_limit)
            )),
            Err(ToolCallAccumulatorError::ResourceLimit)
        );
        assert!(format!("{accumulator:?}").contains("retained_bytes: 0"));
    }

    #[test]
    fn incomplete_terminal_is_distinct_from_transport_eof() {
        let mut accumulator = CanonicalToolCallAccumulator::new();
        accumulator
            .push(&event(
                CanonicalEventType::ToolCallStart,
                Some(1),
                None,
                Some("tool"),
                None,
            ))
            .unwrap();
        assert_eq!(
            accumulator.push(&event(
                CanonicalEventType::ResponseIncomplete,
                None,
                None,
                None,
                None
            )),
            Err(ToolCallAccumulatorError::ResponseIncomplete)
        );
        let mut accumulator = CanonicalToolCallAccumulator::new();
        assert_eq!(
            accumulator.finish(),
            Err(ToolCallAccumulatorError::UnexpectedEof)
        );
    }

    #[test]
    fn all_builtin_stream_adapters_reconstruct_calls_across_every_byte_split() {
        use crate::codec::StreamAdapterKind as Adapter;
        use crate::stream::StreamEventDecoder;

        let fixtures: [(Adapter, &[u8]); 5] = [
            (Adapter::OpenaiChatSse, b"data: {\"choices\":[{\"delta\":{\"tool_calls\":[{\"index\":0,\"id\":\"call\",\"function\":{\"name\":\"lookup\",\"arguments\":\"{}\"}}]},\"finish_reason\":\"tool_calls\"}]}\n\n"),
            (Adapter::OpenaiResponsesSse, b"event: response.output_item.added\ndata: {\"type\":\"response.output_item.added\",\"item\":{\"type\":\"function_call\",\"id\":\"item\",\"call_id\":\"call\",\"name\":\"lookup\",\"arguments\":\"\"}}\n\nevent: response.function_call_arguments.delta\ndata: {\"type\":\"response.function_call_arguments.delta\",\"item_id\":\"item\",\"delta\":\"{}\"}\n\nevent: response.output_item.done\ndata: {\"type\":\"response.output_item.done\",\"item\":{\"type\":\"function_call\",\"id\":\"item\",\"call_id\":\"call\",\"name\":\"lookup\",\"arguments\":\"{}\"}}\n\nevent: response.completed\ndata: {\"type\":\"response.completed\"}\n\n"),
            (Adapter::AnthropicMessagesSse, b"event: content_block_start\ndata: {\"type\":\"content_block_start\",\"index\":0,\"content_block\":{\"type\":\"tool_use\",\"id\":\"call\",\"name\":\"lookup\"}}\n\nevent: content_block_delta\ndata: {\"type\":\"content_block_delta\",\"index\":0,\"delta\":{\"type\":\"input_json_delta\",\"partial_json\":\"{}\"}}\n\nevent: content_block_stop\ndata: {\"type\":\"content_block_stop\",\"index\":0}\n\nevent: message_stop\ndata: {\"type\":\"message_stop\"}\n\n"),
            (Adapter::GeminiInteractionsSse, b"event: step.start\ndata: {\"event_type\":\"step.start\",\"index\":0,\"step\":{\"type\":\"function_call\",\"id\":\"call\",\"name\":\"lookup\"}}\n\nevent: step.delta\ndata: {\"event_type\":\"step.delta\",\"index\":0,\"delta\":{\"type\":\"arguments_delta\",\"arguments\":\"{}\"}}\n\nevent: step.stop\ndata: {\"event_type\":\"step.stop\",\"index\":0}\n\nevent: interaction.completed\ndata: {\"event_type\":\"interaction.completed\",\"interaction\":{\"status\":\"completed\"}}\n\n"),
            (Adapter::GeminiGenerateContentSse, b"data: {\"candidates\":[{\"content\":{\"parts\":[{\"functionCall\":{\"id\":\"call\",\"name\":\"lookup\",\"args\":{\"q\":\"x\"}}}]},\"finishReason\":\"STOP\"}]}\n\n"),
        ];
        for (adapter, bytes) in fixtures {
            for split in 0..=bytes.len() {
                let mut decoder = StreamEventDecoder::new(adapter);
                let mut events = decoder.push(&bytes[..split]).expect("first split");
                events.extend(decoder.push(&bytes[split..]).expect("second split"));
                let (tail, _) = decoder.finalize_events().expect("valid complete stream");
                events.extend(tail);
                let mut accumulator = CanonicalToolCallAccumulator::new();
                let mut calls = Vec::new();
                for event in &events {
                    calls.extend(accumulator.push(event).expect("valid canonical event"));
                }
                assert_eq!(calls.len(), 1, "adapter {adapter:?}, split {split}");
                assert_eq!(
                    calls[0].name, "lookup",
                    "adapter {adapter:?}, split {split}"
                );
                assert!(
                    accumulator.finish().is_ok(),
                    "adapter {adapter:?}, split {split}"
                );
            }
        }
    }
}
