//! Bounded SSE framing and canonical streaming semantics.
//!
//! This module is deliberately below transport and above the finite wire
//! codecs.  It consumes bytes supplied by a caller, emits one bounded batch
//! of canonical events at a time, and reports terminal evidence.  It does
//! not own a socket, timeout, retry, handoff, cancellation, or finalization.

use std::fmt;

use serde_json::{Map, Value, json};
use thiserror::Error;

use super::codec::{CodecError, CodecOutput, CodecReasonCode, StreamAdapterKind};
use super::ir::{
    CacheCounterStatus, CanonicalEvent, CanonicalEventType, CanonicalUsage, ClientSurface,
};

pub const MAX_SSE_FRAME_BYTES: usize = 64 * 1024;
const MAX_PROVIDER_ERROR_MESSAGE_BYTES: usize = 4 * 1024;

/// One assembled SSE event.  Framing does not interpret provider JSON.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SseFrame {
    pub event: Option<String>,
    pub data: String,
    pub fields: Vec<(String, String)>,
    pub is_comment_only: bool,
    pub byte_count: usize,
}

/// Compatibility spelling for callers using the Python class name.
pub type SSEFrame = SseFrame;

#[derive(Debug, Error, Clone, PartialEq, Eq)]
pub enum SseDecodeError {
    #[error("SSE frame exceeded {limit} bytes")]
    FrameTooLarge { limit: usize },
    #[error("SSE decoder was used after EOF")]
    Finished,
}

/// Evidence returned by an SSE decoder at EOF.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SseDecodeResult {
    pub frames: Vec<SseFrame>,
    pub incomplete_frame: bool,
    pub invalid_utf8_replacements: usize,
    pub discarded_frame_count: usize,
}

/// Incremental UTF-8/SSE decoder with an explicit per-record bound.
pub struct SseDecoder {
    max_frame_bytes: usize,
    utf8_pending: Vec<u8>,
    line_buffer: String,
    line_buffer_bytes: usize,
    pending_cr: bool,
    fields: Vec<(String, String)>,
    data_lines: Vec<String>,
    event: Option<String>,
    frame_bytes: usize,
    invalid_utf8_replacements: usize,
    finished: bool,
}

impl fmt::Debug for SseDecoder {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("SseDecoder")
            .field("max_frame_bytes", &self.max_frame_bytes)
            .field("line_buffer_bytes", &self.line_buffer_bytes)
            .field("frame_bytes", &self.frame_bytes)
            .field("fields", &self.fields.len())
            .field("data_lines", &self.data_lines.len())
            .field("invalid_utf8_replacements", &self.invalid_utf8_replacements)
            .finish()
    }
}

impl Default for SseDecoder {
    fn default() -> Self {
        Self::new(MAX_SSE_FRAME_BYTES)
    }
}

impl SseDecoder {
    #[must_use]
    pub const fn new(max_frame_bytes: usize) -> Self {
        Self {
            max_frame_bytes,
            utf8_pending: Vec::new(),
            line_buffer: String::new(),
            line_buffer_bytes: 0,
            pending_cr: false,
            fields: Vec::new(),
            data_lines: Vec::new(),
            event: None,
            frame_bytes: 0,
            invalid_utf8_replacements: 0,
            finished: false,
        }
    }

    /// Consume arbitrary bytes and return only complete records.
    pub fn feed(&mut self, bytes: &[u8]) -> Result<Vec<SseFrame>, SseDecodeError> {
        if self.finished {
            return Err(SseDecodeError::Finished);
        }
        let text = self.decode_utf8(bytes);
        let mut frames = Vec::new();
        for character in text.chars() {
            if self.pending_cr {
                self.pending_cr = false;
                if character == '\n' {
                    self.finish_line(&mut frames)?;
                    continue;
                }
                self.finish_line(&mut frames)?;
            }
            match character {
                '\r' => self.pending_cr = true,
                '\n' => self.finish_line(&mut frames)?,
                character => {
                    self.line_buffer.push(character);
                    self.line_buffer_bytes += character.len_utf8();
                    if self.line_buffer_bytes > self.max_frame_bytes {
                        return Err(SseDecodeError::FrameTooLarge {
                            limit: self.max_frame_bytes,
                        });
                    }
                }
            }
        }
        Ok(frames)
    }

    /// Flush the UTF-8 decoder and the final unterminated SSE record.
    pub fn finish(&mut self) -> Result<SseDecodeResult, SseDecodeError> {
        if self.finished {
            return Ok(SseDecodeResult {
                frames: Vec::new(),
                incomplete_frame: false,
                invalid_utf8_replacements: 0,
                discarded_frame_count: 0,
            });
        }
        self.finished = true;
        let tail = self.decode_utf8_final();
        let mut frames = Vec::new();
        for character in tail.chars() {
            if self.pending_cr {
                self.pending_cr = false;
                if character == '\n' {
                    self.finish_line(&mut frames)?;
                    continue;
                }
                self.finish_line(&mut frames)?;
            }
            match character {
                '\r' => self.pending_cr = true,
                '\n' => self.finish_line(&mut frames)?,
                character => {
                    self.line_buffer.push(character);
                    self.line_buffer_bytes += character.len_utf8();
                }
            }
        }
        if self.pending_cr {
            self.pending_cr = false;
            self.finish_line(&mut frames)?;
        } else if !self.line_buffer.is_empty() {
            self.finish_line(&mut frames)?;
        }
        if !self.fields.is_empty() {
            if let Some(frame) = self.emit_frame() {
                frames.push(frame);
            }
        }
        Ok(SseDecodeResult {
            frames,
            incomplete_frame: false,
            invalid_utf8_replacements: self.invalid_utf8_replacements,
            discarded_frame_count: 0,
        })
    }

    #[must_use]
    pub const fn max_frame_bytes(&self) -> usize {
        self.max_frame_bytes
    }

    #[must_use]
    pub fn line_buffer(&self) -> &str {
        &self.line_buffer
    }

    fn decode_utf8(&mut self, bytes: &[u8]) -> String {
        self.utf8_pending.extend_from_slice(bytes);
        self.decode_utf8_pending(false)
    }

    fn decode_utf8_final(&mut self) -> String {
        self.decode_utf8_pending(true)
    }

    fn decode_utf8_pending(&mut self, final_chunk: bool) -> String {
        let mut output = String::new();
        loop {
            match std::str::from_utf8(&self.utf8_pending) {
                Ok(text) => {
                    output.push_str(text);
                    self.utf8_pending.clear();
                    break;
                }
                Err(error) => {
                    let valid = error.valid_up_to();
                    output.push_str(
                        std::str::from_utf8(&self.utf8_pending[..valid]).unwrap_or_default(),
                    );
                    if let Some(length) = error.error_len() {
                        output.push('\u{fffd}');
                        self.invalid_utf8_replacements += 1;
                        self.utf8_pending.drain(..valid + length);
                    } else if final_chunk {
                        output.push('\u{fffd}');
                        self.invalid_utf8_replacements += 1;
                        self.utf8_pending.clear();
                        break;
                    } else {
                        let remainder = self.utf8_pending.split_off(valid);
                        self.utf8_pending = remainder;
                        break;
                    }
                }
            }
        }
        output
    }

    fn finish_line(&mut self, frames: &mut Vec<SseFrame>) -> Result<(), SseDecodeError> {
        self.frame_bytes = self
            .frame_bytes
            .checked_add(self.line_buffer_bytes + 1)
            .ok_or(SseDecodeError::FrameTooLarge {
                limit: self.max_frame_bytes,
            })?;
        if self.frame_bytes > self.max_frame_bytes {
            return Err(SseDecodeError::FrameTooLarge {
                limit: self.max_frame_bytes,
            });
        }
        let line = std::mem::take(&mut self.line_buffer);
        self.line_buffer_bytes = 0;
        if line.is_empty() {
            if let Some(frame) = self.emit_frame() {
                frames.push(frame);
            }
        } else {
            self.process_line(&line);
        }
        Ok(())
    }

    fn process_line(&mut self, line: &str) {
        if let Some(rest) = line.strip_prefix(':') {
            self.fields.push((String::new(), rest.to_owned()));
            return;
        }
        let (name, value) = line.split_once(':').map_or((line, ""), |(name, value)| {
            (name, value.strip_prefix(' ').unwrap_or(value))
        });
        let name = name.to_owned();
        let value = value.to_owned();
        self.fields.push((name.clone(), value.clone()));
        match name.as_str() {
            "event" => self.event = Some(value),
            "data" => self.data_lines.push(value),
            _ => {}
        }
    }

    fn emit_frame(&mut self) -> Option<SseFrame> {
        if self.fields.is_empty() {
            self.reset_frame();
            return None;
        }
        let fields = std::mem::take(&mut self.fields);
        let data_lines = std::mem::take(&mut self.data_lines);
        let frame = SseFrame {
            event: self.event.take(),
            data: data_lines.join("\n"),
            is_comment_only: fields.iter().all(|(name, _)| name.is_empty()),
            byte_count: self.frame_bytes,
            fields,
        };
        self.frame_bytes = 0;
        Some(frame)
    }

    fn reset_frame(&mut self) {
        self.fields.clear();
        self.data_lines.clear();
        self.event = None;
        self.frame_bytes = 0;
    }
}

/// Provider-native evidence for a stream terminal event.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TerminalEvidence {
    OpenaiDone,
    ResponsesCompleted,
    ResponsesIncomplete,
    ResponsesFailed,
    AnthropicMessageStop,
    GeminiCompleted,
    GeminiIncomplete,
    ProviderError,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StreamTerminalOutcome {
    Success,
    Incomplete,
    ProviderError,
    Malformed,
    EofBeforeBody,
    EofAfterPartialBody,
}

/// Bounded summary consumed by the later coordinator/finalization boundary.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StreamTerminalSummary {
    pub outcome: StreamTerminalOutcome,
    pub evidence: Option<TerminalEvidence>,
    pub saw_payload: bool,
    pub saw_terminal_event: bool,
    pub saw_usage_completion: bool,
    pub missing_final_usage: bool,
    pub incomplete_frame_at_eof: bool,
    pub parser_error_count: usize,
    pub bytes_observed: usize,
    pub usage: Option<CanonicalUsage>,
}

#[derive(Debug, Error)]
pub enum StreamError {
    #[error(transparent)]
    Framing(#[from] SseDecodeError),
    #[error("malformed provider stream event: {0:?}")]
    MalformedEvent(CodecReasonCode),
}

/// Incremental canonical event decoder and usage/terminal observer.
pub struct StreamEventDecoder {
    adapter: StreamAdapterKind,
    framing: SseDecoder,
    usage: UsageAccumulator,
    evidence: Option<TerminalEvidence>,
    saw_payload: bool,
    saw_terminal_event: bool,
    parser_error_count: usize,
    bytes_observed: usize,
    post_terminal_data: bool,
    finalized: bool,
    framing_error: bool,
}

impl fmt::Debug for StreamEventDecoder {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("StreamEventDecoder")
            .field("adapter", &self.adapter)
            .field("saw_payload", &self.saw_payload)
            .field("saw_terminal_event", &self.saw_terminal_event)
            .field("parser_error_count", &self.parser_error_count)
            .field("bytes_observed", &self.bytes_observed)
            .finish()
    }
}

impl StreamEventDecoder {
    #[must_use]
    pub fn new(adapter: StreamAdapterKind) -> Self {
        Self::with_limit(adapter, MAX_SSE_FRAME_BYTES)
    }

    #[must_use]
    pub fn with_limit(adapter: StreamAdapterKind, max_frame_bytes: usize) -> Self {
        Self {
            adapter,
            framing: SseDecoder::new(max_frame_bytes),
            usage: UsageAccumulator::default(),
            evidence: None,
            saw_payload: false,
            saw_terminal_event: false,
            parser_error_count: 0,
            bytes_observed: 0,
            post_terminal_data: false,
            finalized: false,
            framing_error: false,
        }
    }

    pub fn push(&mut self, bytes: &[u8]) -> Result<Vec<CanonicalEvent>, StreamError> {
        if self.finalized {
            return Err(StreamError::Framing(SseDecodeError::Finished));
        }
        self.bytes_observed = self.bytes_observed.saturating_add(bytes.len());
        let frames = match self.framing.feed(bytes) {
            Ok(frames) => frames,
            Err(error) => {
                self.framing_error = true;
                return Err(error.into());
            }
        };
        self.decode_frames(frames)
    }

    pub fn finalize(&mut self) -> Result<StreamTerminalSummary, StreamError> {
        let (_, summary) = self.finalize_events()?;
        Ok(summary)
    }

    /// Flush framing and return events found in the final partial record.
    pub fn finalize_events(
        &mut self,
    ) -> Result<(Vec<CanonicalEvent>, StreamTerminalSummary), StreamError> {
        if self.finalized {
            return Ok((Vec::new(), self.summary(false)));
        }
        self.finalized = true;
        let result = match self.framing.finish() {
            Ok(result) => result,
            Err(error) => {
                self.framing_error = true;
                return Err(error.into());
            }
        };
        let events = self.decode_frames(result.frames)?;
        Ok((events, self.summary(result.incomplete_frame)))
    }

    /// Alias matching the terminology used by the Python stream boundary.
    pub fn finish(&mut self) -> Result<(Vec<CanonicalEvent>, StreamTerminalSummary), StreamError> {
        self.finalize_events()
    }

    #[must_use]
    pub fn usage(&self) -> Option<CanonicalUsage> {
        self.usage.value()
    }

    fn decode_frames(&mut self, frames: Vec<SseFrame>) -> Result<Vec<CanonicalEvent>, StreamError> {
        let mut events = Vec::new();
        for frame in frames {
            if frame.is_comment_only || !frame.fields.iter().any(|(name, _)| name == "data") {
                continue;
            }
            if !frame.data.is_empty() {
                self.saw_payload = true;
            }
            if self.saw_terminal_event && !frame.data.is_empty() {
                self.post_terminal_data = true;
            }
            let mut frame_value = Map::new();
            frame_value.insert("data".into(), Value::String(frame.data.clone()));
            if let Some(event) = &frame.event {
                frame_value.insert("event".into(), Value::String(event.clone()));
            }
            let decoded = match decode_stream_event(self.adapter, &Value::Object(frame_value)) {
                Ok(decoded) => decoded.value,
                Err(error) => {
                    self.parser_error_count += 1;
                    self.framing_error = true;
                    return Err(StreamError::MalformedEvent(error.reason));
                }
            };
            self.observe_terminal(&frame, &decoded);
            for event in decoded {
                if let Some(usage) = event.usage.as_ref() {
                    self.usage
                        .merge(usage, matches!(event.event_type, CanonicalEventType::Usage));
                }
                events.push(event);
            }
        }
        Ok(events)
    }

    fn observe_terminal(&mut self, frame: &SseFrame, events: &[CanonicalEvent]) {
        let event_name = frame.event.as_deref().unwrap_or_default();
        let data_done = frame.data.trim() == "[DONE]";
        let evidence = match self.adapter {
            StreamAdapterKind::OpenaiChatSse if data_done => Some(TerminalEvidence::OpenaiDone),
            StreamAdapterKind::AnthropicMessagesSse if event_name == "message_stop" => {
                Some(TerminalEvidence::AnthropicMessageStop)
            }
            StreamAdapterKind::OpenaiResponsesSse => match event_name {
                "response.completed" => Some(TerminalEvidence::ResponsesCompleted),
                "response.incomplete" => Some(TerminalEvidence::ResponsesIncomplete),
                "response.failed" => Some(TerminalEvidence::ResponsesFailed),
                "error" => Some(TerminalEvidence::ProviderError),
                _ => None,
            },
            StreamAdapterKind::GeminiInteractionsSse if event_name == "interaction.completed" => {
                let status = events
                    .iter()
                    .find_map(|event| event.finish_reason.as_deref());
                Some(if matches!(status, Some("completed" | "requires_action")) {
                    TerminalEvidence::GeminiCompleted
                } else {
                    TerminalEvidence::GeminiIncomplete
                })
            }
            StreamAdapterKind::GeminiGenerateContentSse => {
                let finish = events
                    .iter()
                    .find_map(|event| event.finish_reason.as_deref());
                finish.map(|reason| {
                    if reason == "STOP" {
                        TerminalEvidence::GeminiCompleted
                    } else {
                        TerminalEvidence::GeminiIncomplete
                    }
                })
            }
            _ if event_name == "error" => Some(TerminalEvidence::ProviderError),
            _ => None,
        };
        if let Some(evidence) = evidence {
            if self.saw_terminal_event {
                self.post_terminal_data = true;
            } else {
                self.evidence = Some(evidence);
                self.saw_terminal_event = true;
            }
        }
        if events
            .iter()
            .any(|event| event.event_type == CanonicalEventType::Error)
        {
            self.evidence = Some(TerminalEvidence::ProviderError);
            self.saw_terminal_event = true;
        }
    }

    fn summary(&self, incomplete_frame_at_eof: bool) -> StreamTerminalSummary {
        let outcome = if self.framing_error || self.parser_error_count > 0 {
            StreamTerminalOutcome::Malformed
        } else {
            match self.evidence {
                Some(
                    TerminalEvidence::OpenaiDone
                    | TerminalEvidence::ResponsesCompleted
                    | TerminalEvidence::AnthropicMessageStop
                    | TerminalEvidence::GeminiCompleted,
                ) => StreamTerminalOutcome::Success,
                Some(
                    TerminalEvidence::ResponsesIncomplete
                    | TerminalEvidence::ResponsesFailed
                    | TerminalEvidence::GeminiIncomplete,
                ) => StreamTerminalOutcome::Incomplete,
                Some(TerminalEvidence::ProviderError) => StreamTerminalOutcome::ProviderError,
                None if !self.saw_payload => StreamTerminalOutcome::EofBeforeBody,
                None => StreamTerminalOutcome::EofAfterPartialBody,
            }
        };
        let usage = self.usage.value();
        StreamTerminalSummary {
            outcome,
            evidence: self.evidence,
            saw_payload: self.saw_payload,
            saw_terminal_event: self.saw_terminal_event,
            saw_usage_completion: self.usage.complete,
            missing_final_usage: matches!(outcome, StreamTerminalOutcome::Success)
                && !self.usage.complete,
            incomplete_frame_at_eof,
            parser_error_count: self.parser_error_count + usize::from(self.post_terminal_data),
            bytes_observed: self.bytes_observed,
            usage,
        }
    }
}

#[derive(Debug, Default)]
struct UsageAccumulator {
    value: CanonicalUsage,
    present: bool,
    complete: bool,
}

impl UsageAccumulator {
    fn merge(&mut self, incoming: &CanonicalUsage, complete: bool) {
        self.present = true;
        self.complete |= complete;
        self.value.input_tokens = add_optional(self.value.input_tokens, incoming.input_tokens);
        self.value.output_tokens = add_optional(self.value.output_tokens, incoming.output_tokens);
        self.value.total_tokens = add_optional(self.value.total_tokens, incoming.total_tokens);
        self.value.cached_input_tokens =
            add_optional(self.value.cached_input_tokens, incoming.cached_input_tokens);
        self.value.cache_read_input_tokens = add_optional(
            self.value.cache_read_input_tokens,
            incoming.cache_read_input_tokens,
        );
        self.value.cache_creation_input_tokens = add_optional(
            self.value.cache_creation_input_tokens,
            incoming.cache_creation_input_tokens,
        );
        self.value.cache_write_input_tokens = add_optional(
            self.value.cache_write_input_tokens,
            incoming.cache_write_input_tokens,
        );
        self.value.reasoning_tokens =
            add_optional(self.value.reasoning_tokens, incoming.reasoning_tokens);
        if incoming.cache_counter_status == CacheCounterStatus::Reported {
            self.value.cache_counter_status = CacheCounterStatus::Reported;
        } else if self.value.cache_counter_status == CacheCounterStatus::UnknownFormat {
            self.value.cache_counter_status = incoming.cache_counter_status;
        }
    }

    fn value(&self) -> Option<CanonicalUsage> {
        self.present.then(|| self.value.clone())
    }
}

fn add_optional(left: Option<u64>, right: Option<u64>) -> Option<u64> {
    match (left, right) {
        (Some(left), Some(right)) => Some(left.saturating_add(right)),
        (Some(value), None) | (None, Some(value)) => Some(value),
        (None, None) => None,
    }
}

fn bounded_string(value: Option<&Value>, limit: usize) -> Option<String> {
    value.and_then(Value::as_str).map(|text| {
        if text.len() <= limit {
            text.to_owned()
        } else {
            let mut end = limit;
            while !text.is_char_boundary(end) {
                end -= 1;
            }
            text[..end].to_owned()
        }
    })
}

fn string(value: Option<&Value>) -> Option<String> {
    value.and_then(Value::as_str).map(ToOwned::to_owned)
}

fn index(value: Option<&Value>) -> Option<usize> {
    value
        .and_then(Value::as_u64)
        .and_then(|value| usize::try_from(value).ok())
}

fn object(value: Option<&Value>) -> Option<&Map<String, Value>> {
    value.and_then(Value::as_object)
}

fn array(value: Option<&Value>) -> Option<&Vec<Value>> {
    value.and_then(Value::as_array)
}

fn event_name(frame: &Map<String, Value>, payload: &Map<String, Value>) -> Option<String> {
    string(frame.get("event"))
        .or_else(|| string(payload.get("type")))
        .or_else(|| string(payload.get("event_type")))
}

fn payload_value(frame: &Value) -> Result<(Map<String, Value>, Option<String>), CodecError> {
    let frame_object = frame
        .as_object()
        .ok_or_else(|| CodecError::new(CodecReasonCode::MalformedProviderEvent))?;
    let data = frame_object.get("data").unwrap_or(frame);
    if data.as_str() == Some("[DONE]") {
        return Ok((Map::new(), Some("[DONE]".into())));
    }
    let payload = match data {
        Value::Object(object) => object.clone(),
        Value::String(text) => serde_json::from_str::<Value>(text)
            .map_err(|_| CodecError::new(CodecReasonCode::MalformedProviderEvent))?
            .as_object()
            .cloned()
            .ok_or_else(|| CodecError::new(CodecReasonCode::MalformedProviderEvent))?,
        _ => return Err(CodecError::new(CodecReasonCode::MalformedProviderEvent)),
    };
    Ok((payload, None))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UsageProtocol {
    Openai,
    Anthropic,
    Gemini,
}

fn usage_from(value: Option<&Value>, protocol: UsageProtocol) -> Option<CanonicalUsage> {
    let object = value?.as_object()?;
    let mut usage = CanonicalUsage {
        cache_counter_status: CacheCounterStatus::NotReported,
        ..CanonicalUsage::default()
    };
    match protocol {
        UsageProtocol::Openai => {
            usage.input_tokens = token(
                object
                    .get("prompt_tokens")
                    .or_else(|| object.get("input_tokens")),
            );
            usage.output_tokens = token(
                object
                    .get("completion_tokens")
                    .or_else(|| object.get("output_tokens")),
            );
            usage.total_tokens = token(object.get("total_tokens"));
            if let Some(details) = object
                .get("prompt_tokens_details")
                .and_then(Value::as_object)
            {
                usage.cache_read_input_tokens = token(details.get("cached_tokens"));
                usage.cached_input_tokens = usage.cache_read_input_tokens;
                usage.cache_write_input_tokens = token(details.get("cache_write_tokens"));
            }
            if usage.cache_read_input_tokens.is_none() {
                usage.cache_read_input_tokens = token(object.get("cache_read_input_tokens"));
                usage.cached_input_tokens = usage.cache_read_input_tokens;
            }
            if usage.cache_read_input_tokens.is_some() || usage.cache_write_input_tokens.is_some() {
                usage.cache_counter_status = CacheCounterStatus::Reported;
            }
            if let Some(details) = object
                .get("completion_tokens_details")
                .and_then(Value::as_object)
            {
                usage.reasoning_tokens = token(details.get("reasoning_tokens"));
            }
            if usage.total_tokens.is_none()
                || (usage.total_tokens == Some(0)
                    && (usage.input_tokens.unwrap_or(0) != 0
                        || usage.output_tokens.unwrap_or(0) != 0))
            {
                usage.total_tokens = add_optional(usage.input_tokens, usage.output_tokens);
            }
        }
        UsageProtocol::Anthropic => {
            usage.input_tokens = token(
                object
                    .get("input_tokens")
                    .or_else(|| object.get("prompt_tokens")),
            );
            usage.output_tokens = token(
                object
                    .get("output_tokens")
                    .or_else(|| object.get("completion_tokens")),
            );
            usage.cache_read_input_tokens = token(object.get("cache_read_input_tokens"));
            usage.cache_creation_input_tokens = token(object.get("cache_creation_input_tokens"));
            if usage.cache_read_input_tokens.is_some()
                || usage.cache_creation_input_tokens.is_some()
            {
                usage.cache_counter_status = CacheCounterStatus::Reported;
                usage.cached_input_tokens = add_optional(
                    usage.cache_read_input_tokens,
                    usage.cache_creation_input_tokens,
                );
                usage.cache_write_input_tokens = usage.cache_creation_input_tokens;
            }
            usage.total_tokens = add_optional(
                add_optional(usage.input_tokens, usage.output_tokens),
                usage.cached_input_tokens,
            );
        }
        UsageProtocol::Gemini => {
            usage.input_tokens = token(
                object
                    .get("total_input_tokens")
                    .or_else(|| object.get("promptTokenCount")),
            );
            usage.output_tokens = token(
                object
                    .get("total_output_tokens")
                    .or_else(|| object.get("candidatesTokenCount")),
            );
            usage.total_tokens = token(
                object
                    .get("total_tokens")
                    .or_else(|| object.get("totalTokenCount")),
            );
            if usage.total_tokens.is_none() {
                usage.total_tokens = add_optional(usage.input_tokens, usage.output_tokens);
            }
        }
    }
    Some(usage)
}

/// Normalize one provider usage object while preserving absent/unknown shape.
///
/// `None` means the provider omitted usage entirely.  `Some` with
/// `UnknownFormat` means a usage value was present but was not an object the
/// selected provider family understands.  Numeric zero remains `Some(0)`.
pub fn normalize_usage(value: Option<&Value>, protocol: UsageProtocol) -> Option<CanonicalUsage> {
    let value = value?;
    if !value.is_object() {
        return Some(CanonicalUsage::default());
    }
    usage_from(Some(value), protocol)
}

fn token(value: Option<&Value>) -> Option<u64> {
    value.and_then(Value::as_u64)
}

fn canonical_event(event_type: CanonicalEventType) -> CanonicalEvent {
    CanonicalEvent {
        event_type,
        response_id: None,
        model: None,
        index: None,
        delta: None,
        call_id: None,
        name: None,
        arguments: None,
        finish_reason: None,
        usage: None,
        error_type: None,
        error_message: None,
    }
}

/// Decode the provider-specific JSON carried by one framed SSE event.
pub fn decode_stream_event(
    adapter: StreamAdapterKind,
    frame: &Value,
) -> Result<CodecOutput<Vec<CanonicalEvent>>, CodecError> {
    let frame_object = frame
        .as_object()
        .ok_or_else(|| CodecError::new(CodecReasonCode::MalformedProviderEvent))?;
    let (payload, marker) = payload_value(frame)?;
    let name = event_name(frame_object, &payload);
    let mut events = Vec::new();
    if marker.as_deref() == Some("[DONE]") {
        if adapter == StreamAdapterKind::OpenaiChatSse {
            events.push(canonical_event(CanonicalEventType::ResponseComplete));
        }
        return Ok(CodecOutput::new(events));
    }
    match adapter {
        StreamAdapterKind::OpenaiChatSse => decode_openai_chat(frame_object, &payload, &mut events),
        StreamAdapterKind::OpenaiResponsesSse => {
            decode_openai_responses(frame_object, &payload, &mut events)
        }
        StreamAdapterKind::AnthropicMessagesSse => {
            decode_anthropic(frame_object, &payload, &mut events)
        }
        StreamAdapterKind::GeminiInteractionsSse => {
            decode_interactions(frame_object, &payload, &mut events)
        }
        StreamAdapterKind::GeminiGenerateContentSse => {
            decode_generate_content(&payload, &mut events)
        }
    }
    let _ = name;
    Ok(CodecOutput::new(events))
}

fn decode_openai_chat(
    frame: &Map<String, Value>,
    payload: &Map<String, Value>,
    events: &mut Vec<CanonicalEvent>,
) {
    if frame.get("data").and_then(Value::as_str) == Some("[DONE]") {
        return;
    }
    if frame.get("event").and_then(Value::as_str) == Some("error") || payload.contains_key("error")
    {
        events.push(error_event(payload));
        return;
    }
    let choices = array(payload.get("choices"));
    if choices.is_none() || choices.is_some_and(Vec::is_empty) {
        if let Some(usage) = usage_from(payload.get("usage"), UsageProtocol::Openai) {
            events.push(CanonicalEvent {
                usage: Some(usage),
                ..canonical_event(CanonicalEventType::Usage)
            });
        }
        return;
    }
    let Some(choice) = choices
        .and_then(|items| items.first())
        .and_then(Value::as_object)
    else {
        return;
    };
    if let Some(delta) = choice.get("delta").and_then(Value::as_object) {
        if let Some(text) = string(delta.get("content")) {
            if !text.is_empty() {
                events.push(CanonicalEvent {
                    delta: Some(text),
                    ..canonical_event(CanonicalEventType::TextDelta)
                });
            }
        }
        if let Some(text) = string(delta.get("reasoning_content")) {
            if !text.is_empty() {
                events.push(CanonicalEvent {
                    delta: Some(text),
                    ..canonical_event(CanonicalEventType::ReasoningDelta)
                });
            }
        }
        if let Some(calls) = array(delta.get("tool_calls")) {
            for call in calls.iter().filter_map(Value::as_object) {
                let function = call.get("function").and_then(Value::as_object);
                let idx = index(call.get("index"));
                let call_id = string(call.get("id"));
                let name = function.and_then(|function| string(function.get("name")));
                let arguments = function.and_then(|function| string(function.get("arguments")));
                if call_id.is_some() || name.is_some() {
                    events.push(CanonicalEvent {
                        index: idx,
                        call_id: call_id.clone(),
                        name,
                        ..canonical_event(CanonicalEventType::ToolCallStart)
                    });
                }
                if arguments.is_some() {
                    events.push(CanonicalEvent {
                        index: idx,
                        call_id,
                        delta: arguments,
                        ..canonical_event(CanonicalEventType::ToolCallArgumentsDelta)
                    });
                }
            }
        }
    }
    if let Some(reason) = string(choice.get("finish_reason")) {
        events.push(CanonicalEvent {
            finish_reason: Some(reason),
            ..canonical_event(CanonicalEventType::ResponseComplete)
        });
    }
}

fn decode_openai_responses(
    _frame: &Map<String, Value>,
    payload: &Map<String, Value>,
    events: &mut Vec<CanonicalEvent>,
) {
    let name = string(_frame.get("event"))
        .or_else(|| string(payload.get("type")))
        .unwrap_or_default();
    match name.as_str() {
        "response.created" => {
            let response = object(payload.get("response")).unwrap_or(payload);
            let mut event = canonical_event(CanonicalEventType::ResponseStart);
            event.response_id = string(response.get("id"));
            event.model = string(response.get("model"));
            events.push(event);
        }
        "response.output_text.delta" | "response.reasoning_summary_text.delta" => {
            let mut event = canonical_event(if name.contains("reasoning") {
                CanonicalEventType::ReasoningDelta
            } else {
                CanonicalEventType::TextDelta
            });
            event.delta = string(payload.get("delta"));
            if event.delta.is_some() {
                events.push(event);
            }
        }
        "response.output_item.added" => {
            if let Some(item) = object(payload.get("item"))
                && item.get("type").and_then(Value::as_str) == Some("function_call")
            {
                let mut event = canonical_event(CanonicalEventType::ToolCallStart);
                event.call_id = string(item.get("call_id")).or_else(|| string(item.get("id")));
                event.name = string(item.get("name"));
                events.push(event);
            }
        }
        "response.function_call_arguments.delta" => {
            let mut event = canonical_event(CanonicalEventType::ToolCallArgumentsDelta);
            event.call_id = string(payload.get("item_id"));
            event.delta = string(payload.get("delta"));
            events.push(event);
        }
        "response.output_item.done" => {
            if let Some(item) = object(payload.get("item"))
                && item.get("type").and_then(Value::as_str) == Some("function_call")
            {
                let mut event = canonical_event(CanonicalEventType::ToolCallStop);
                event.call_id = string(item.get("call_id")).or_else(|| string(item.get("id")));
                events.push(event);
            }
        }
        "response.completed" | "response.incomplete" | "response.failed" => {
            let response = object(payload.get("response")).unwrap_or(payload);
            if let Some(usage) = usage_from(response.get("usage"), UsageProtocol::Openai) {
                events.push(CanonicalEvent {
                    usage: Some(usage),
                    ..canonical_event(CanonicalEventType::Usage)
                });
            }
            let mut event = canonical_event(if name == "response.completed" {
                CanonicalEventType::ResponseComplete
            } else {
                CanonicalEventType::ResponseIncomplete
            });
            event.finish_reason = Some(name.strip_prefix("response.").unwrap_or(&name).to_owned());
            events.push(event);
        }
        "error" => events.push(error_event(payload)),
        _ => {}
    }
}

fn decode_anthropic(
    frame: &Map<String, Value>,
    payload: &Map<String, Value>,
    events: &mut Vec<CanonicalEvent>,
) {
    let name = string(frame.get("event"))
        .or_else(|| string(payload.get("type")))
        .unwrap_or_default();
    match name.as_str() {
        "message_start" => {
            let message = object(payload.get("message"));
            let mut event = canonical_event(CanonicalEventType::ResponseStart);
            if let Some(message) = message {
                event.response_id = string(message.get("id"));
                event.model = string(message.get("model"));
            }
            events.push(event);
        }
        "content_block_start" => {
            let mut event = canonical_event(CanonicalEventType::ContentStart);
            event.index = index(payload.get("index"));
            if let Some(block) = object(payload.get("content_block"))
                && block.get("type").and_then(Value::as_str) == Some("tool_use")
            {
                event.event_type = CanonicalEventType::ToolCallStart;
                event.call_id = string(block.get("id"));
                event.name = string(block.get("name"));
            }
            events.push(event);
        }
        "content_block_delta" => {
            let Some(delta) = object(payload.get("delta")) else {
                return;
            };
            let kind = delta
                .get("type")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let mut event = canonical_event(match kind {
                "text_delta" => CanonicalEventType::TextDelta,
                "thinking_delta" => CanonicalEventType::ReasoningDelta,
                "input_json_delta" => CanonicalEventType::ToolCallArgumentsDelta,
                _ => return,
            });
            event.index = index(payload.get("index"));
            event.delta = string(delta.get("text"))
                .or_else(|| string(delta.get("thinking")))
                .or_else(|| string(delta.get("partial_json")));
            events.push(event);
        }
        "content_block_stop" => {
            let mut event = canonical_event(CanonicalEventType::ContentStop);
            event.index = index(payload.get("index"));
            events.push(event);
        }
        "message_delta" => {
            let stop =
                object(payload.get("delta")).and_then(|delta| string(delta.get("stop_reason")));
            if let Some(usage) = usage_from(payload.get("usage"), UsageProtocol::Anthropic) {
                events.push(CanonicalEvent {
                    usage: Some(usage),
                    ..canonical_event(CanonicalEventType::Usage)
                });
            }
            if let Some(stop) = stop {
                events.push(CanonicalEvent {
                    finish_reason: Some(stop),
                    ..canonical_event(CanonicalEventType::ResponseComplete)
                });
            }
        }
        "message_stop" => events.push(canonical_event(CanonicalEventType::ResponseComplete)),
        "error" => events.push(error_event(payload)),
        _ => {}
    }
}

fn decode_interactions(
    frame: &Map<String, Value>,
    payload: &Map<String, Value>,
    events: &mut Vec<CanonicalEvent>,
) {
    let name = string(frame.get("event"))
        .or_else(|| string(payload.get("event_type")))
        .unwrap_or_default();
    match name.as_str() {
        "interaction.created" => {
            let interaction = object(payload.get("interaction")).unwrap_or(payload);
            let mut event = canonical_event(CanonicalEventType::ResponseStart);
            event.response_id = string(interaction.get("id"));
            event.model = string(interaction.get("model"));
            events.push(event);
        }
        "step.start" => {
            let mut event = canonical_event(CanonicalEventType::ContentStart);
            event.index = index(payload.get("index"));
            if let Some(step) = object(payload.get("step"))
                && step.get("type").and_then(Value::as_str) == Some("function_call")
            {
                event.event_type = CanonicalEventType::ToolCallStart;
                event.call_id = string(step.get("id"));
                event.name = string(step.get("name"));
            }
            events.push(event);
        }
        "step.delta" => {
            let Some(delta) = object(payload.get("delta")) else {
                return;
            };
            let kind = delta
                .get("type")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let mut event = canonical_event(match kind {
                "text" => CanonicalEventType::TextDelta,
                "thought_summary" => CanonicalEventType::ReasoningDelta,
                "arguments_delta" => CanonicalEventType::ToolCallArgumentsDelta,
                _ => return,
            });
            event.index = index(payload.get("index"));
            event.delta = string(delta.get("text"))
                .or_else(|| {
                    object(delta.get("content")).and_then(|content| string(content.get("text")))
                })
                .or_else(|| string(delta.get("arguments")));
            events.push(event);
        }
        "step.stop" => {
            let mut event = canonical_event(CanonicalEventType::ContentStop);
            event.index = index(payload.get("index"));
            events.push(event);
        }
        "interaction.completed" => {
            let interaction = object(payload.get("interaction")).unwrap_or(payload);
            if let Some(usage) = usage_from(interaction.get("usage"), UsageProtocol::Gemini) {
                events.push(CanonicalEvent {
                    usage: Some(usage),
                    ..canonical_event(CanonicalEventType::Usage)
                });
            }
            let status = string(interaction.get("status"));
            events.push(CanonicalEvent {
                finish_reason: status.clone(),
                ..canonical_event(
                    if matches!(status.as_deref(), Some("completed" | "requires_action")) {
                        CanonicalEventType::ResponseComplete
                    } else {
                        CanonicalEventType::ResponseIncomplete
                    },
                )
            });
        }
        "error" => events.push(error_event(payload)),
        _ => {}
    }
}

fn decode_generate_content(payload: &Map<String, Value>, events: &mut Vec<CanonicalEvent>) {
    if let Some(candidate) = array(payload.get("candidates"))
        .and_then(|items| items.first())
        .and_then(Value::as_object)
    {
        if let Some(parts) =
            object(candidate.get("content")).and_then(|content| array(content.get("parts")))
        {
            for part in parts.iter().filter_map(Value::as_object) {
                if let Some(text) = string(part.get("text")) {
                    events.push(CanonicalEvent {
                        delta: Some(text),
                        ..canonical_event(
                            if part.get("thought").and_then(Value::as_bool) == Some(true) {
                                CanonicalEventType::ReasoningDelta
                            } else {
                                CanonicalEventType::TextDelta
                            },
                        )
                    });
                }
                if let Some(call) = object(part.get("functionCall")) {
                    let mut start = canonical_event(CanonicalEventType::ToolCallStart);
                    start.name = string(call.get("name"));
                    events.push(start);
                    let mut args = canonical_event(CanonicalEventType::ToolCallArgumentsDelta);
                    args.name = string(call.get("name"));
                    args.arguments = string(call.get("args")).or_else(|| {
                        serde_json::to_string(call.get("args").unwrap_or(&Value::Null)).ok()
                    });
                    args.delta = args.arguments.clone();
                    events.push(args);
                }
            }
        }
    }
    if let Some(usage) = usage_from(payload.get("usageMetadata"), UsageProtocol::Gemini) {
        events.push(CanonicalEvent {
            usage: Some(usage),
            ..canonical_event(CanonicalEventType::Usage)
        });
    }
    if let Some(candidate) = array(payload.get("candidates"))
        .and_then(|items| items.first())
        .and_then(Value::as_object)
    {
        if let Some(reason) = string(candidate.get("finishReason")) {
            events.push(CanonicalEvent {
                finish_reason: Some(reason),
                ..canonical_event(
                    if candidate.get("finishReason").and_then(Value::as_str) == Some("STOP") {
                        CanonicalEventType::ResponseComplete
                    } else {
                        CanonicalEventType::ResponseIncomplete
                    },
                )
            });
        }
    }
}

fn error_event(payload: &Map<String, Value>) -> CanonicalEvent {
    let error = object(payload.get("error")).unwrap_or(payload);
    CanonicalEvent {
        error_type: bounded_string(error.get("type"), 256),
        error_message: bounded_string(error.get("message"), MAX_PROVIDER_ERROR_MESSAGE_BYTES),
        ..canonical_event(CanonicalEventType::Error)
    }
}

fn sse(event: Option<&str>, payload: &Value) -> Vec<u8> {
    let mut out = Vec::new();
    if let Some(event) = event {
        out.extend_from_slice(b"event: ");
        out.extend_from_slice(event.as_bytes());
        out.extend_from_slice(b"\n");
    }
    out.extend_from_slice(b"data: ");
    out.extend_from_slice(payload.to_string().as_bytes());
    out.extend_from_slice(b"\n\n");
    out
}

fn usage_value(usage: &CanonicalUsage, protocol: UsageProtocol) -> Value {
    let mut out = Map::new();
    match protocol {
        UsageProtocol::Openai => {
            put_token(&mut out, "prompt_tokens", usage.input_tokens);
            put_token(&mut out, "completion_tokens", usage.output_tokens);
            put_token(&mut out, "total_tokens", usage.total_tokens);
            let mut details = Map::new();
            put_token(&mut details, "cached_tokens", usage.cache_read_input_tokens);
            put_token(
                &mut details,
                "cache_write_tokens",
                usage.cache_write_input_tokens,
            );
            if !details.is_empty() {
                out.insert("prompt_tokens_details".into(), Value::Object(details));
            }
        }
        UsageProtocol::Anthropic => {
            put_token(&mut out, "input_tokens", usage.input_tokens);
            put_token(&mut out, "output_tokens", usage.output_tokens);
            put_token(
                &mut out,
                "cache_read_input_tokens",
                usage.cache_read_input_tokens,
            );
            put_token(
                &mut out,
                "cache_creation_input_tokens",
                usage.cache_creation_input_tokens,
            );
        }
        UsageProtocol::Gemini => {
            put_token(&mut out, "total_input_tokens", usage.input_tokens);
            put_token(&mut out, "total_output_tokens", usage.output_tokens);
            put_token(&mut out, "total_tokens", usage.total_tokens);
        }
    }
    Value::Object(out)
}

fn put_token(out: &mut Map<String, Value>, key: &str, value: Option<u64>) {
    if let Some(value) = value {
        out.insert(key.into(), Value::from(value));
    }
}

/// Encode a canonical event to one of the three public client SSE grammars.
pub fn encode_client_event(
    client: ClientSurface,
    event: &CanonicalEvent,
) -> Result<Vec<u8>, CodecError> {
    let bytes = match client {
        ClientSurface::ChatCompletions => encode_chat_event(event),
        ClientSurface::Responses => encode_responses_event(event),
        ClientSurface::Messages => encode_anthropic_event(event),
    }?;
    Ok(bytes)
}

fn encode_chat_event(event: &CanonicalEvent) -> Result<Vec<u8>, CodecError> {
    if event.event_type == CanonicalEventType::ResponseComplete {
        return Ok(b"data: [DONE]\n\n".to_vec());
    }
    let mut delta = Map::new();
    match event.event_type {
        CanonicalEventType::TextDelta => {
            delta.insert(
                "content".into(),
                Value::String(event.delta.clone().unwrap_or_default()),
            );
        }
        CanonicalEventType::ReasoningDelta => {
            delta.insert(
                "reasoning_content".into(),
                Value::String(event.delta.clone().unwrap_or_default()),
            );
        }
        CanonicalEventType::ToolCallStart => {
            delta.insert("tool_calls".into(), json!([{"index":event.index.unwrap_or(0),"id":event.call_id.clone().unwrap_or_default(),"type":"function","function":{"name":event.name.clone().unwrap_or_default(),"arguments":""}}]));
        }
        CanonicalEventType::ToolCallArgumentsDelta => {
            delta.insert("tool_calls".into(), json!([{"index":event.index.unwrap_or(0),"function":{"arguments":event.delta.clone().unwrap_or_default()}}]));
        }
        CanonicalEventType::Usage => {}
        CanonicalEventType::ResponseIncomplete => {}
        CanonicalEventType::Error => {
            return Ok(sse(
                None,
                &json!({"error":{"type":event.error_type.clone().unwrap_or_else(|| "error".into()),"message":event.error_message.clone().unwrap_or_default()}}),
            ));
        }
        _ => return Ok(Vec::new()),
    }
    let mut choice = Map::new();
    choice.insert("index".into(), Value::from(event.index.unwrap_or(0)));
    choice.insert("delta".into(), Value::Object(delta));
    choice.insert(
        "finish_reason".into(),
        event
            .finish_reason
            .clone()
            .map_or(Value::Null, Value::String),
    );
    let mut payload = Map::new();
    payload.insert(
        "id".into(),
        Value::String(event.response_id.clone().unwrap_or_default()),
    );
    payload.insert(
        "object".into(),
        Value::String("chat.completion.chunk".into()),
    );
    payload.insert(
        "model".into(),
        Value::String(event.model.clone().unwrap_or_default()),
    );
    payload.insert("choices".into(), Value::Array(vec![Value::Object(choice)]));
    if event.event_type == CanonicalEventType::Usage {
        if let Some(usage) = &event.usage {
            payload.insert("usage".into(), usage_value(usage, UsageProtocol::Openai));
        }
    }
    Ok(sse(None, &Value::Object(payload)))
}

fn encode_responses_event(event: &CanonicalEvent) -> Result<Vec<u8>, CodecError> {
    let (name, payload) = match event.event_type {
        CanonicalEventType::ResponseComplete => (
            "response.completed",
            json!({"type":"response.completed","response":{"id":event.response_id.clone().unwrap_or_default(),"status":"completed","usage":event.usage.as_ref().map(|u| usage_value(u, UsageProtocol::Openai))}}),
        ),
        CanonicalEventType::ResponseIncomplete => (
            "response.incomplete",
            json!({"type":"response.incomplete","response":{"id":event.response_id.clone().unwrap_or_default(),"status":event.finish_reason.clone().unwrap_or_else(|| "incomplete".into())}}),
        ),
        CanonicalEventType::Error => (
            "error",
            json!({"type":"error","error":{"type":event.error_type.clone().unwrap_or_else(|| "error".into()),"message":event.error_message.clone().unwrap_or_default()}}),
        ),
        CanonicalEventType::TextDelta => (
            "response.output_text.delta",
            json!({"type":"response.output_text.delta","delta":event.delta.clone().unwrap_or_default()}),
        ),
        CanonicalEventType::ReasoningDelta => (
            "response.reasoning_summary_text.delta",
            json!({"type":"response.reasoning_summary_text.delta","delta":event.delta.clone().unwrap_or_default()}),
        ),
        CanonicalEventType::ToolCallStart => (
            "response.output_item.added",
            json!({"type":"response.output_item.added","item":{"type":"function_call","call_id":event.call_id.clone().unwrap_or_default(),"name":event.name.clone().unwrap_or_default(),"arguments":""}}),
        ),
        CanonicalEventType::ToolCallArgumentsDelta => (
            "response.function_call_arguments.delta",
            json!({"type":"response.function_call_arguments.delta","item_id":event.call_id.clone().unwrap_or_default(),"delta":event.delta.clone().unwrap_or_default()}),
        ),
        CanonicalEventType::ToolCallStop => (
            "response.output_item.done",
            json!({"type":"response.output_item.done","item":{"type":"function_call","call_id":event.call_id.clone().unwrap_or_default()}}),
        ),
        CanonicalEventType::Usage => return Ok(Vec::new()),
        _ => return Ok(Vec::new()),
    };
    Ok(sse(Some(name), &payload))
}

fn encode_anthropic_event(event: &CanonicalEvent) -> Result<Vec<u8>, CodecError> {
    let (name, payload) = match event.event_type {
        CanonicalEventType::ResponseComplete => ("message_stop", json!({"type":"message_stop"})),
        CanonicalEventType::TextDelta => (
            "content_block_delta",
            json!({"type":"content_block_delta","index":event.index.unwrap_or(0),"delta":{"type":"text_delta","text":event.delta.clone().unwrap_or_default()}}),
        ),
        CanonicalEventType::ReasoningDelta => (
            "content_block_delta",
            json!({"type":"content_block_delta","index":event.index.unwrap_or(0),"delta":{"type":"thinking_delta","thinking":event.delta.clone().unwrap_or_default()}}),
        ),
        CanonicalEventType::ToolCallArgumentsDelta => (
            "content_block_delta",
            json!({"type":"content_block_delta","index":event.index.unwrap_or(0),"delta":{"type":"input_json_delta","partial_json":event.delta.clone().unwrap_or_default()}}),
        ),
        CanonicalEventType::Usage => {
            return Ok(sse(
                Some("message_delta"),
                &json!({"type":"message_delta","usage":event.usage.as_ref().map(|u| usage_value(u, UsageProtocol::Anthropic)).unwrap_or(Value::Object(Map::new()))}),
            ));
        }
        CanonicalEventType::ToolCallStart => (
            "content_block_start",
            json!({"type":"content_block_start","index":event.index.unwrap_or(0),"content_block":{"type":"tool_use","id":event.call_id.clone().unwrap_or_default(),"name":event.name.clone().unwrap_or_default(),"input":{}}}),
        ),
        CanonicalEventType::ContentStart => (
            "content_block_start",
            json!({"type":"content_block_start","index":event.index.unwrap_or(0),"content_block":{"type":"text","text":""}}),
        ),
        CanonicalEventType::ContentStop | CanonicalEventType::ToolCallStop => (
            "content_block_stop",
            json!({"type":"content_block_stop","index":event.index.unwrap_or(0)}),
        ),
        CanonicalEventType::Error => (
            "error",
            json!({"type":"error","error":{"type":event.error_type.clone().unwrap_or_else(|| "error".into()),"message":event.error_message.clone().unwrap_or_default()}}),
        ),
        _ => return Ok(Vec::new()),
    };
    Ok(sse(Some(name), &payload))
}

impl fmt::Display for TerminalEvidence {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "{:?}", self)
    }
}
