//! Bounded OpenAI-compatible embedding request and response semantics.
//!
//! Embeddings do not pass through the dialogue IR. This module owns their
//! finite, text-only wire contract and keeps input text out of diagnostics.

use base64::{Engine as _, engine::general_purpose::STANDARD};
use bytes::Bytes;
use serde_json::{Map, Value, json};
use thiserror::Error;

pub const MAX_EMBEDDING_BODY_BYTES: usize = 64 * 1024;
pub const MAX_EMBEDDING_INPUTS: usize = 32;
pub const MAX_EMBEDDING_ITEM_BYTES: usize = 16 * 1024;
pub const MAX_EMBEDDING_TOTAL_INPUT_BYTES: usize = 64 * 1024;
pub const MAX_EMBEDDING_DIMENSIONS: usize = 8192;
pub const MAX_EMBEDDING_RESPONSE_BYTES: usize = 8 * 1024 * 1024;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EmbeddingEncoding {
    Float,
    Base64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum EmbeddingInput {
    Single(String),
    Multiple(Vec<String>),
}

impl EmbeddingInput {
    pub fn values(&self) -> Vec<&str> {
        match self {
            Self::Single(value) => vec![value],
            Self::Multiple(values) => values.iter().map(String::as_str).collect(),
        }
    }

    pub fn len(&self) -> usize {
        match self {
            Self::Single(_) => 1,
            Self::Multiple(values) => values.len(),
        }
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EmbeddingRequest {
    pub model: String,
    pub input: EmbeddingInput,
    pub dimensions: Option<usize>,
    pub encoding_format: EmbeddingEncoding,
}

impl EmbeddingRequest {
    pub fn parse(body: &[u8]) -> Result<Self, EmbeddingRequestError> {
        if body.len() > MAX_EMBEDDING_BODY_BYTES {
            return Err(EmbeddingRequestError::BodyTooLarge);
        }
        let value: Value =
            serde_json::from_slice(body).map_err(|_| EmbeddingRequestError::InvalidJson)?;
        Self::parse_value(&value)
    }

    pub fn parse_value(value: &Value) -> Result<Self, EmbeddingRequestError> {
        let object = value
            .as_object()
            .ok_or(EmbeddingRequestError::InvalidShape)?;
        if object.keys().any(|key| {
            !matches!(
                key.as_str(),
                "model" | "input" | "dimensions" | "encoding_format"
            )
        }) {
            return Err(EmbeddingRequestError::UnsupportedField);
        }
        let model = object
            .get("model")
            .and_then(Value::as_str)
            .filter(|value| !value.trim().is_empty() && value.len() <= 256)
            .ok_or(EmbeddingRequestError::InvalidModel)?
            .to_owned();
        let input = match object
            .get("input")
            .ok_or(EmbeddingRequestError::InvalidInput)?
        {
            Value::String(value) => EmbeddingInput::Single(value.clone()),
            Value::Array(values) => {
                if values.len() > MAX_EMBEDDING_INPUTS {
                    return Err(EmbeddingRequestError::TooManyInputs);
                }
                let values = values
                    .iter()
                    .map(|value| {
                        value
                            .as_str()
                            .map(str::to_owned)
                            .ok_or(EmbeddingRequestError::InvalidInput)
                    })
                    .collect::<Result<Vec<_>, _>>()?;
                EmbeddingInput::Multiple(values)
            }
            _ => return Err(EmbeddingRequestError::InvalidInput),
        };
        if input.is_empty()
            || input
                .values()
                .iter()
                .any(|value| value.is_empty() || value.len() > MAX_EMBEDDING_ITEM_BYTES)
        {
            return Err(EmbeddingRequestError::InvalidInput);
        }
        let total_bytes = input
            .values()
            .iter()
            .map(|value| value.len())
            .sum::<usize>();
        if total_bytes > MAX_EMBEDDING_TOTAL_INPUT_BYTES {
            return Err(EmbeddingRequestError::InputTooLarge);
        }
        let encoding_format = match object.get("encoding_format") {
            None => EmbeddingEncoding::Float,
            Some(Value::String(value)) if value == "float" => EmbeddingEncoding::Float,
            Some(Value::String(value)) if value == "base64" => EmbeddingEncoding::Base64,
            Some(_) => return Err(EmbeddingRequestError::UnsupportedEncoding),
        };
        let dimensions = match object.get("dimensions") {
            None => None,
            Some(Value::Number(value)) => value
                .as_u64()
                .and_then(|value| usize::try_from(value).ok())
                .filter(|value| (1..=MAX_EMBEDDING_DIMENSIONS).contains(value))
                .map(Some)
                .ok_or(EmbeddingRequestError::InvalidDimensions)?,
            Some(_) => return Err(EmbeddingRequestError::InvalidDimensions),
        };
        Ok(Self {
            model,
            input,
            dimensions,
            encoding_format,
        })
    }

    /// Produce the provider request after the coordinator has selected an
    /// explicitly qualified embedding model. The caller supplies that model's
    /// configured upstream identifier; client model aliases never reach it.
    pub fn upstream_body(&self, upstream_model: &str) -> Result<Bytes, EmbeddingRequestError> {
        let mut object = Map::new();
        object.insert("model".into(), Value::String(upstream_model.to_owned()));
        let input = match &self.input {
            EmbeddingInput::Single(value) => Value::String(value.clone()),
            EmbeddingInput::Multiple(values) => json!(values),
        };
        object.insert("input".into(), input);
        if let Some(dimensions) = self.dimensions {
            object.insert("dimensions".into(), json!(dimensions));
        }
        object.insert("encoding_format".into(), Value::String("float".into()));
        serde_json::to_vec(&Value::Object(object))
            .map(Bytes::from)
            .map_err(|_| EmbeddingRequestError::InvalidJson)
    }

    pub fn input_bytes(&self) -> usize {
        self.input.values().iter().map(|value| value.len()).sum()
    }
}

#[derive(Debug, Error, Clone, Copy, PartialEq, Eq)]
pub enum EmbeddingRequestError {
    #[error("embedding request body is too large")]
    BodyTooLarge,
    #[error("embedding request JSON is invalid")]
    InvalidJson,
    #[error("embedding request has an invalid shape")]
    InvalidShape,
    #[error("embedding request uses an unsupported field")]
    UnsupportedField,
    #[error("embedding model is invalid")]
    InvalidModel,
    #[error("embedding input is invalid")]
    InvalidInput,
    #[error("embedding request has too many inputs")]
    TooManyInputs,
    #[error("embedding input exceeds the byte limit")]
    InputTooLarge,
    #[error("embedding request uses an unsupported encoding")]
    UnsupportedEncoding,
    #[error("embedding dimensions are invalid")]
    InvalidDimensions,
    #[error("embedding provider response is invalid")]
    InvalidResponse,
    #[error("embedding provider response is too large")]
    ResponseTooLarge,
}

#[derive(Debug, Clone, PartialEq)]
pub struct EmbeddingResponse {
    pub body: Bytes,
    pub input_tokens: Option<u64>,
}

impl EmbeddingResponse {
    pub fn parse(
        body: &[u8],
        request: &EmbeddingRequest,
        client_model: &str,
    ) -> Result<Self, EmbeddingRequestError> {
        if body.len() > MAX_EMBEDDING_RESPONSE_BYTES {
            return Err(EmbeddingRequestError::ResponseTooLarge);
        }
        let value: Value =
            serde_json::from_slice(body).map_err(|_| EmbeddingRequestError::InvalidResponse)?;
        let object = value
            .as_object()
            .ok_or(EmbeddingRequestError::InvalidResponse)?;
        if object.get("object").and_then(Value::as_str) != Some("list")
            || !object
                .get("model")
                .and_then(Value::as_str)
                .is_some_and(|model| !model.is_empty() && model.len() <= 256)
        {
            return Err(EmbeddingRequestError::InvalidResponse);
        }
        let data = object
            .get("data")
            .and_then(Value::as_array)
            .ok_or(EmbeddingRequestError::InvalidResponse)?;
        if data.len() != request.input.len() {
            return Err(EmbeddingRequestError::InvalidResponse);
        }
        let expected_dimensions = request.dimensions;
        let mut indexed = Vec::with_capacity(data.len());
        let mut observed_dimensions = None;
        for item in data {
            let row = item
                .as_object()
                .ok_or(EmbeddingRequestError::InvalidResponse)?;
            if row.get("object").and_then(Value::as_str) != Some("embedding") {
                return Err(EmbeddingRequestError::InvalidResponse);
            }
            let index = row
                .get("index")
                .and_then(Value::as_u64)
                .and_then(|index| usize::try_from(index).ok())
                .filter(|index| *index < data.len())
                .ok_or(EmbeddingRequestError::InvalidResponse)?;
            let vector = row
                .get("embedding")
                .and_then(Value::as_array)
                .ok_or(EmbeddingRequestError::InvalidResponse)?;
            if vector.is_empty()
                || vector.len() > MAX_EMBEDDING_DIMENSIONS
                || expected_dimensions.is_some_and(|dimensions| dimensions != vector.len())
                || observed_dimensions.is_some_and(|dimensions| dimensions != vector.len())
                || vector
                    .iter()
                    .any(|value| value.as_f64().is_none_or(|number| !number.is_finite()))
            {
                return Err(EmbeddingRequestError::InvalidResponse);
            }
            observed_dimensions = Some(vector.len());
            let mut item = item.clone();
            if request.encoding_format == EmbeddingEncoding::Base64 {
                let mut encoded = Vec::with_capacity(vector.len() * std::mem::size_of::<f32>());
                for value in vector {
                    let number = value
                        .as_f64()
                        .ok_or(EmbeddingRequestError::InvalidResponse)?
                        as f32;
                    if !number.is_finite() {
                        return Err(EmbeddingRequestError::InvalidResponse);
                    }
                    encoded.extend_from_slice(&number.to_le_bytes());
                }
                item.as_object_mut()
                    .ok_or(EmbeddingRequestError::InvalidResponse)?
                    .insert("embedding".into(), Value::String(STANDARD.encode(encoded)));
            }
            indexed.push((index, item));
        }
        indexed.sort_by_key(|(index, _)| *index);
        if indexed
            .iter()
            .enumerate()
            .any(|(expected, (index, _))| expected != *index)
        {
            return Err(EmbeddingRequestError::InvalidResponse);
        }
        // Some compatible backends omit usage. Keep that fact unknown rather
        // than inventing a token count; malformed usage, when present, is an
        // invalid provider response.
        let prompt_tokens = match object.get("usage") {
            None | Some(Value::Null) => None,
            Some(value) => {
                let usage = value
                    .as_object()
                    .ok_or(EmbeddingRequestError::InvalidResponse)?;
                let prompt_tokens = usage
                    .get("prompt_tokens")
                    .and_then(Value::as_u64)
                    .ok_or(EmbeddingRequestError::InvalidResponse)?;
                let total_tokens = usage
                    .get("total_tokens")
                    .and_then(Value::as_u64)
                    .ok_or(EmbeddingRequestError::InvalidResponse)?;
                if total_tokens < prompt_tokens {
                    return Err(EmbeddingRequestError::InvalidResponse);
                }
                Some(prompt_tokens)
            }
        };
        let mut result = object.clone();
        result.insert(
            "data".into(),
            Value::Array(indexed.into_iter().map(|(_, row)| row).collect()),
        );
        result.insert("model".into(), Value::String(client_model.to_owned()));
        let output = serde_json::to_vec(&Value::Object(result))
            .map_err(|_| EmbeddingRequestError::InvalidResponse)?;
        if output.len() > MAX_EMBEDDING_RESPONSE_BYTES {
            return Err(EmbeddingRequestError::ResponseTooLarge);
        }
        Ok(Self {
            body: Bytes::from(output),
            input_tokens: prompt_tokens,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_single_and_array_text_with_float_and_base64_encoding() {
        let single = EmbeddingRequest::parse(br#"{"model":"p/m","input":"hello"}"#).unwrap();
        assert_eq!(single.input.len(), 1);
        let multiple = EmbeddingRequest::parse(
            br#"{"model":"p/m","input":["a","b"],"dimensions":2,"encoding_format":"float"}"#,
        )
        .unwrap();
        let body: Value = serde_json::from_slice(&multiple.upstream_body("m").unwrap()).unwrap();
        assert_eq!(body["input"], json!(["a", "b"]));
        assert_eq!(body["dimensions"], 2);
        let base64 = EmbeddingRequest::parse(
            br#"{"model":"p/m","input":["a","b"],"encoding_format":"base64"}"#,
        )
        .unwrap();
        assert_eq!(base64.encoding_format, EmbeddingEncoding::Base64);
        let upstream: Value = serde_json::from_slice(&base64.upstream_body("m").unwrap()).unwrap();
        assert_eq!(upstream["model"], "m");
        assert_eq!(upstream["input"], json!(["a", "b"]));
        assert_eq!(upstream["encoding_format"], "float");
    }

    #[test]
    fn rejects_unsupported_shapes_and_unbounded_input() {
        for body in [
            br#"{"model":"m","input":[]}"#.as_slice(),
            br#"{"model":"m","input":"x","stream":true}"#,
            br#"{"model":"m","input":"x","encoding_format":"binary"}"#,
            br#"{"model":"m","input":{},"dimensions":0}"#,
        ] {
            assert!(EmbeddingRequest::parse(body).is_err());
        }
        let huge = format!(
            r#"{{"model":"m","input":"{}"}}"#,
            "x".repeat(MAX_EMBEDDING_ITEM_BYTES + 1)
        );
        assert_eq!(
            EmbeddingRequest::parse(huge.as_bytes()),
            Err(EmbeddingRequestError::InvalidInput)
        );
    }

    #[test]
    fn validates_vector_count_dimensions_finiteness_usage_and_order() {
        let request =
            EmbeddingRequest::parse(br#"{"model":"p/m","input":["a","b"],"dimensions":2}"#)
                .unwrap();
        let response = br#"{"object":"list","model":"upstream","data":[{"object":"embedding","index":1,"embedding":[2,3]},{"object":"embedding","index":0,"embedding":[0,1]}],"usage":{"prompt_tokens":3,"total_tokens":3}}"#;
        let parsed = EmbeddingResponse::parse(response, &request, "p/m").unwrap();
        let value: Value = serde_json::from_slice(&parsed.body).unwrap();
        assert_eq!(value["data"][0]["index"], 0);
        assert_eq!(value["model"], "p/m");
        let no_usage = br#"{"object":"list","model":"m","data":[{"object":"embedding","index":0,"embedding":[0,1]},{"object":"embedding","index":1,"embedding":[2,3]}]}"#;
        let parsed = EmbeddingResponse::parse(no_usage, &request, "p/m").unwrap();
        let value: Value = serde_json::from_slice(&parsed.body).unwrap();
        assert!(parsed.input_tokens.is_none());
        assert!(value.get("usage").is_none());
        let non_finite = br#"{"object":"list","model":"m","data":[{"object":"embedding","index":0,"embedding":[NaN,2]},{"object":"embedding","index":1,"embedding":[1,2]}],"usage":{"prompt_tokens":3,"total_tokens":3}}"#;
        assert_eq!(
            EmbeddingResponse::parse(non_finite, &request, "p/m"),
            Err(EmbeddingRequestError::InvalidResponse)
        );
        let missing = br#"{"object":"list","model":"m","data":[{"object":"embedding","index":0,"embedding":[1,2]}],"usage":{"prompt_tokens":3,"total_tokens":3}}"#;
        assert_eq!(
            EmbeddingResponse::parse(missing, &request, "p/m"),
            Err(EmbeddingRequestError::InvalidResponse)
        );
    }
}
