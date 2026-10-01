use dst_server::{events, model::ErrorCode};
use serde_json::{Value, json};

fn record() -> Value {
    json!({
        "v": 3, "nonce": "01ARZ3NDEKTSV4RRFFQ69G5FAV", "generation": 1,
        "session_id": "SESSION", "seq": 1, "event": "dst.world.state_changed",
        "tick": 10, "monotonic_ms": 20, "cycle": null,
        "data": {"name": "cycles", "value": 9_007_199_254_740_991_u64}
    })
}

#[test]
fn frozen_schema_covers_every_producer_and_preserves_values() {
    assert_eq!(
        events::schema()["discriminator"]["mapping"]
            .as_object()
            .unwrap()
            .len(),
        54
    );
    let value = record();
    assert_eq!(events::parse(&value.to_string()).unwrap(), value);
    let mut boolean = record();
    boolean["data"] = json!({"name": "israining", "value": false});
    assert_eq!(events::parse(&boolean.to_string()).unwrap(), boolean);

    let mut migration = record();
    migration["event"] = json!("dst.player.migration_started");
    migration["data"] = json!({
        "player": {"prefab": "wilson", "guid": 9_007_199_254_740_991_u64,
            "userid": "KU_TEST", "position": {"x": 1.0, "y": 0.0, "z": -2.5}},
        "destination_shard_id": "2", "portal_id": null,
        "destination": {"x": 0, "y": 0, "z": 0}
    });
    assert_eq!(events::parse(&migration.to_string()).unwrap(), migration);
}

#[test]
fn event_variants_reject_bad_shapes_and_unsafe_native_counters() {
    for (field, value) in [
        ("v", json!(1)),
        ("nonce", json!("8".repeat(26))),
        ("nonce", json!("01arz3ndektsv4rrffq69g5fav")),
        ("generation", json!(-1)),
        ("generation", json!(true)),
        ("generation", json!(1.0)),
        ("session_id", json!("")),
        ("seq", json!(0)),
        ("tick", json!("10")),
        ("tick", json!(10.0)),
        ("cycle", json!(9_007_199_254_740_992_u64)),
        ("monotonic_ms", json!(1e50)),
        ("event", json!("dst.unknown")),
        ("unexpected", json!(false)),
        ("data", json!({"name": "season", "value": 1})),
        ("data", json!({"name": "cycles", "value": true})),
        ("data", json!({"name": "cycles", "value": 1.0})),
        (
            "data",
            json!({"name": "cycles", "value": 1, "unexpected": null}),
        ),
    ] {
        let mut invalid = record();
        invalid[field] = value;
        assert_eq!(
            events::validate(&invalid).unwrap_err().code,
            ErrorCode::Invalid,
            "{invalid}"
        );
    }
    for field in ["session_id", "cycle", "data"] {
        let mut invalid = record();
        invalid.as_object_mut().unwrap().remove(field);
        assert!(events::validate(&invalid).is_err());
    }
}

#[test]
fn diagnostics_are_sanitized_and_bounded() {
    let mut diagnostic = record();
    diagnostic["event"] = json!("dst.telemetry.error");
    diagnostic["data"] =
        json!({"stage": "player.finishedwork", "message": "callback_failed", "count": 1});
    events::validate(&diagnostic).unwrap();
    for (field, value) in [
        ("message", json!("SECRET_TOKEN")),
        ("stage", json!("callback\nprivate chat")),
        ("count", json!(0)),
    ] {
        let mut invalid = diagnostic.clone();
        invalid["data"][field] = value;
        let error = events::validate(&invalid).unwrap_err();
        assert!(!error.to_string().contains("SECRET_TOKEN"));
        assert!(!error.to_string().contains("private chat"));
    }
    assert!(events::parse("{}").is_err());
    assert!(
        !events::parse("not json SECRET_TOKEN")
            .unwrap_err()
            .message
            .contains("SECRET_TOKEN")
    );
    assert!(events::parse(&" ".repeat(events::MAX_EVENT_BYTES + 1)).is_err());
    let mut large = record();
    large["data"] = json!({"name": "season", "value": "x".repeat(events::MAX_EVENT_BYTES)});
    assert!(events::validate(&large).is_err());
    let mut nonfinite = record();
    nonfinite["data"]["value"] = serde_json::from_str("1e400").unwrap();
    assert!(events::validate(&nonfinite).is_err());
}
