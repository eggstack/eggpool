//! Pure inbound request admission, sizing, and body preparation.
//!
//! This module deliberately stops before routing or provider submission.  It
//! owns the bounded parse and the values that later layers may safely reuse.

pub mod admission;
pub mod body;
pub mod limits;

pub use admission::{
    AdmissionError, AdmissionOptions, AdmittedRequest, StaticRoutingFacts, admit_request,
    affinity_identity_input, canonical_request_from_value, routing_request_facts,
};
pub use body::{
    BodyEncodingError, EncodedJsonBody, encode_compact_json, encode_compact_json_bounded,
};
pub use limits::{
    CONTEXT_ESTIMATE_MIN_TOKENS, DEFAULT_MAX_REQUEST_BODY_BYTES, LimitError,
    MAX_ESTIMATED_INPUT_TOKENS, MAX_IMAGE_BYTES, MAX_PDF_BYTES, MAX_SSE_FRAME_BYTES,
    base64_definitely_exceeds, estimate_context_input_tokens, estimate_input_tokens,
    estimate_json_value_tokens, estimate_reservation_tokens, requested_output_tokens,
    validate_base64,
};
