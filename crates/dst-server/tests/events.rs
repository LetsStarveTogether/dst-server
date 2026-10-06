use dst_server::{events, model::ErrorCode};
use serde_json::{Value, json};

fn record() -> Value {
    json!({
        "nonce": "01ARZ3NDEKTSV4RRFFQ69G5FAV", "generation": 1,
        "session_id": "SESSION", "seq": 1, "event": "dst.world.state_changed",
        "tick": 10, "monotonic_ms": 20, "cycle": null,
        "data": {"name": "cycles", "value": 9_007_199_254_740_991_u64}
    })
}

#[test]
fn preserves_world_time_and_native_identity_without_precision_loss() {
    for data in [
        record()["data"].clone(),
        json!({"name": "israining", "value": false}),
    ] {
        let mut event = record();
        event["data"] = data;
        assert_eq!(
            events::parse(event.to_string()).unwrap().into_value(),
            event
        );
    }

    let mut death = record();
    death["event"] = json!("dst.entity.death");
    death["data"] = json!({
        "victim": {"prefab": "wilson", "guid": 9_007_199_254_740_991_u64, "userid": "KU_TEST"},
        "afflicter": null, "attributed_userid": null, "age_seconds": 4800.25,
        "is_epic": false, "cause": "hunger", "corpsing": false,
        "position": {"x": 1.0, "y": 0, "z": -2.5}
    });
    assert_eq!(
        events::parse(death.to_string()).unwrap().into_value(),
        death
    );
    for invalid_age in [json!(-1.0), serde_json::from_str("1e400").unwrap()] {
        death["data"]["age_seconds"] = invalid_age;
        assert!(events::validate(&death).is_err());
    }
}

#[test]
fn accepts_unknown_load_state_and_rejects_missing_or_malformed_load_evidence() {
    let mut snapshot = record();
    snapshot["event"] = json!("dst.server.presence");
    snapshot["data"] = json!({
        "reason": "startup", "clients": ["KU_TEST"],
        "players": [{"userid": "KU_TEST", "prefab": "wilson", "is_ghost": false, "loaded": null}],
        "world": {"season": "autumn", "phase": "day", "moonphase": "full", "nightmarephase": null,
            "israining": false, "issnowing": false, "isacidraining": null, "islunarhailing": null,
            "lunar_rifts_enabled": true, "shadow_rifts_enabled": null}
    });
    for state in [Value::Null, json!(false), json!(true)] {
        snapshot["data"]["players"][0]["loaded"] = state;
        assert_eq!(
            events::parse(snapshot.to_string()).unwrap().into_value(),
            snapshot
        );
    }
    snapshot["data"]["players"][0]["loaded"] = json!(1);
    assert!(events::validate(&snapshot).is_err());
    snapshot["data"]["players"][0]
        .as_object_mut()
        .unwrap()
        .remove("loaded");
    assert!(events::validate(&snapshot).is_err());
}

#[test]
fn event_variants_reject_bad_shapes_and_unsafe_native_counters() {
    for (field, value) in [
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
        assert_eq!(
            events::parse(invalid.to_string()).unwrap_err().code,
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
fn validates_byte_limit_before_accepting_an_event() {
    let padded = format!(
        "{:width$}",
        record().to_string(),
        width = events::MAX_EVENT_BYTES
    );
    assert_eq!(events::parse(&padded).unwrap().into_value(), record());
    assert!(events::parse(&(padded + " ")).is_err());
    let mut large = record();
    large["event"] = json!("dst.player.public_chat");
    large["data"] = json!({"userid": null, "prefab": null, "message": "", "emote": false,
        "name": "x".repeat(events::MAX_EVENT_BYTES)});
    assert_eq!(
        events::validate(&large).unwrap_err().details["reason"],
        "length"
    );
}

#[test]
fn parse_errors_keep_fixed_reasons_and_json_positions() {
    let mut invalid = record();
    invalid["data"] = json!({"SECRET_TOKEN": "private chat"});
    for (source, reason, position) in [
        (vec![b' '; events::MAX_EVENT_BYTES + 1], "length", false),
        (br#"{"SECRET_TOKEN":"#.to_vec(), "json_eof", true),
        (b"{invalid SECRET_TOKEN}".to_vec(), "json_syntax", true),
        (
            format!("{}0{}", "[".repeat(66), "]".repeat(66)).into_bytes(),
            "complexity",
            false,
        ),
        (serde_json::to_vec(&invalid).unwrap(), "schema", false),
    ] {
        let error = events::parse(source).unwrap_err();
        assert_eq!(error.details["reason"], reason);
        assert_eq!(error.details["position"].is_object(), position);
        assert!(!error.message.contains("SECRET_TOKEN"));
        if position {
            assert!(error.details["position"]["line"].as_u64().unwrap() >= 1);
            assert!(error.details["position"]["column"].as_u64().unwrap() >= 1);
        }
    }
    assert!(events::parse(record().to_string()).is_ok());
}
