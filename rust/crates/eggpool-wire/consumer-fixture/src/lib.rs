use eggpool_wire::{
    CanonicalToolCallAccumulator, RequestEncodeOptions, encode_request_for_surface,
    ir::{CanonicalMessage, CanonicalRequest, CanonicalRole},
    profile::WireSurface,
};

pub fn encode_semantic_request() {
    let request = CanonicalRequest::from_canonical(
        "model",
        vec![CanonicalMessage {
            role: CanonicalRole::User,
            content: Vec::new(),
            tool_call_id: None,
            name: None,
            refusal: None,
        }],
    );
    encode_request_for_surface(
        &request,
        WireSurface::OpenaiChatCompletions,
        &RequestEncodeOptions::default(),
    )
    .expect("built-in encoding")
    .value;
}

pub fn independent_accumulator() -> CanonicalToolCallAccumulator {
    CanonicalToolCallAccumulator::new()
}
