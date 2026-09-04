#[path = "../build_support.rs"]
mod build_support;

#[test]
fn semantically_equivalent_reformatted_manifest_is_accepted() {
    let original = include_str!("../../src/eggpool/db/schema/checksums.json");
    let value: serde_json::Value = serde_json::from_str(original).expect("canonical JSON");
    let reformatted = serde_json::to_string(&value).expect("compact JSON");

    let parsed = build_support::parse_checksums(&reformatted);
    assert_eq!(parsed.len(), 54);
    assert_eq!(
        parsed.get("0001_initial.sql").map(String::as_str),
        Some("2aa5800147fc3ef8cc35591c44564244dba138b4dbc92fc1779e4f88813912ad")
    );
}

#[test]
fn malformed_checksum_content_is_rejected() {
    for manifest in [
        r#"{"files":{"0001_initial.sql":1}}"#,
        r#"{"files":{"0001_initial.sql":"not-a-sha"}}"#,
        r#"{"files":{"0001_initial.sql":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","0001_initial.sql":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}}"#,
    ] {
        assert!(
            std::panic::catch_unwind(|| build_support::parse_checksums(manifest)).is_err(),
            "manifest should be rejected: {manifest}"
        );
    }
}
