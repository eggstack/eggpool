-- Q012 canonical model-info fixture, version 1.
--
-- Apply after q012-populated.sql. Values are fixed, sanitized public metadata;
-- no source refresh or external provider request is needed.

INSERT INTO model_info_canonical
  (model_id, status, summary, detail_json, provenance_json, conflicts_json,
   sparse, first_seen_at, last_seen_at, last_refreshed_at, next_refresh_at)
VALUES
  ('q012-chat-model', 'fresh', 'Fixture model summary <safe>',
   '{"display_name":"Q012 Chat Model","providers":["q012-alpha"],"limits":{"effective_context":32768,"external_context":65536,"effective_output":4096,"external_output":8192},"modalities":["text","image"],"supports_tools":true,"family":"q012-family","license":"MIT","release_date":"2026-01-01","external_ids":{"openrouter":"q012/chat-model"},"benchmarks":[{"name":"Artificial Analysis Intelligence","score":42.5,"rank":7,"percentile":0.91,"version":"2026-01","notes":"fixture benchmark","source":"artificial_analysis","observed_at":"2026-09-01T00:00:00Z"}],"huggingface_metadata":{"downloads":123,"likes":7,"pipeline_tag":"text-generation","library_name":"transformers","license":"mit","tags":["text-generation","fixture"]}}',
   '{"sources":["provider_catalog","artificial_analysis"],"reconciled_at":"2026-09-01T00:00:00Z"}',
   '{"family":{"sources":{"provider_catalog":"q012-family","manual_override":"fixture-family"},"selected":"fixture-family","reason":"operator override"}}',
   0, '2026-09-01 00:00:00', '2026-09-01 00:00:00',
   '2026-09-01 00:00:00', '2026-10-03 00:00:00');

INSERT INTO model_info_observations
  (model_id, provider_id, source, source_model_id, observed_at, expires_at,
   confidence, raw_hash, normalized_json, raw_json)
VALUES
  ('q012-chat-model', 'q012-alpha', 'artificial_analysis', 'q012-chat-model',
   '2026-09-01T00:00:00Z', NULL, 0.95,
   'q012-fixture-observation-hash-0000000000000000000000000000000000000000',
   '{"display_name":"Q012 Chat Model","context_window":32768,"max_output_tokens":4096,"modalities":["text","image"]}',
   '{"fixture":"sanitized public metadata"}');
