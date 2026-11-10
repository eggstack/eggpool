//! Retired `[tools]` execution switches fail closed. A false or absent
//! section still loads.

use eggpool::{Config, config::local_metadata_violations};

#[test]
fn false_tool_switches_load_and_each_true_switch_is_rejected() {
    Config::default()
        .validate()
        .expect("absent tools section validates");
    let mut disabled = Config::default();
    disabled.tools.enabled = false;
    disabled.tools.gateway_enabled = false;
    disabled.tools.hosted_execution_enabled = false;
    disabled.validate().expect("false tools switches validate");

    for field in [
        "tools.enabled",
        "tools.gateway_enabled",
        "tools.hosted_execution_enabled",
    ] {
        let mut config = Config::default();
        config.tools.base_url = "http://user:secret-value@127.0.0.1:11320/mcp".to_owned();
        match field {
            "tools.enabled" => config.tools.enabled = true,
            "tools.gateway_enabled" => config.tools.gateway_enabled = true,
            _ => config.tools.hosted_execution_enabled = true,
        }
        let message = config.validate().expect_err(field).to_string();
        assert!(message.contains(field), "{message}");
        assert!(!message.contains("secret-value"), "{message}");
    }
}

#[test]
fn local_example_has_no_outbound_metadata_sources() {
    let config = Config::from_toml(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/config.local.example.toml"
    ))
    .expect("local example parses");
    assert!(local_metadata_violations(&config).is_empty());
    let mut openrouter = config.clone();
    openrouter.model_info.sources.openrouter.enabled = true;
    assert_eq!(
        local_metadata_violations(&openrouter),
        vec!["model_info.sources.openrouter.enabled"]
    );
    let mut pricing = config.clone();
    pricing.pricing.catalogs.opencode_zen.enabled = true;
    assert_eq!(
        local_metadata_violations(&pricing),
        vec!["pricing.catalogs.opencode_zen.enabled"]
    );
    let mut updates = config;
    updates.update_checker.enabled = true;
    assert_eq!(
        local_metadata_violations(&updates),
        vec!["update_checker.enabled"]
    );
}
