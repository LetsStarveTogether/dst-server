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
fn event_types_cover_every_producer_and_preserve_values() {
    assert_eq!(events::EVENT_NAMES.len(), 56);
    let value = record();
    assert_eq!(
        events::parse(value.to_string()).unwrap().into_value(),
        value
    );
    let mut boolean = record();
    boolean["data"] = json!({"name": "israining", "value": false});
    assert_eq!(
        events::parse(boolean.to_string()).unwrap().into_value(),
        boolean
    );

    let mut migration = record();
    migration["event"] = json!("dst.player.migration_started");
    migration["data"] = json!({
        "player": {"prefab": "wilson", "guid": 9_007_199_254_740_991_u64,
            "userid": "KU_TEST", "position": {"x": 1.0, "y": 0.0, "z": -2.5}},
        "destination_shard_id": "2", "portal_id": null,
        "destination": {"x": 0, "y": 0, "z": 0}
    });
    assert_eq!(
        events::parse(migration.to_string()).unwrap().into_value(),
        migration
    );
}

#[test]
fn identity_events_preserve_steam_ids_without_numeric_conversion() {
    let client = json!({"userid": "KU_TEST", "steam_id": null, "name": "玩家", "admin": false, "moderator": null});
    for (event, data, identity_path) in [
        ("dst.client.authenticated", client.clone(), "/data"),
        ("dst.client.disconnected", client.clone(), "/data"),
        (
            "dst.player.loaded",
            json!({
                "player": {"prefab": "wilson", "guid": 42, "userid": "KU_TEST", "position": null},
                "steam_id": null, "name": "玩家", "is_ghost": false, "age_seconds": 4800.25
            }),
            "/data",
        ),
        (
            "dst.server.presence",
            json!({
                "clients": [client], "players": [], "max_players": 9, "reason": "startup",
                "world": {"season":"autumn","phase":"day","moonphase":"full","nightmarephase":null,
                    "israining":false,"issnowing":false,"isacidraining":null,"islunarhailing":null},
                "health": {"errors": 0, "events_emitted": 0, "generation": 1, "last_error": null,
                    "protocol": 3, "telemetry_status": "active"}
            }),
            "/data/clients/0",
        ),
    ] {
        let mut value = record();
        value["event"] = json!(event);
        value["data"] = data;
        for steam_id in [
            Value::Null,
            json!("76561197960265729"),
            json!("76561198000000001"),
            json!("76561202255233023"),
        ] {
            value.pointer_mut(identity_path).unwrap()["steam_id"] = steam_id;
            assert_eq!(
                events::parse(value.to_string()).unwrap().into_value(),
                value
            );
        }
        for steam_id in [
            json!(76_561_198_000_000_001_u64),
            json!(false),
            json!(""),
            json!("0"),
            json!("76561197960265728"),
            json!("76561202255233024"),
            json!("12345678901234567"),
            json!("1000000000000000001"),
            json!("7656119800000000x"),
            json!(" 76561198000000001"),
        ] {
            value.pointer_mut(identity_path).unwrap()["steam_id"] = steam_id;
            assert!(events::validate(&value).is_err(), "{value}");
            assert!(events::parse(value.to_string()).is_err(), "{value}");
        }
        value
            .pointer_mut(identity_path)
            .unwrap()
            .as_object_mut()
            .unwrap()
            .remove("steam_id");
        assert!(events::validate(&value).is_err(), "{value}");
    }
}

#[test]
fn tracking_metadata_preserves_optional_values_and_rejects_invalid_types() {
    let mut client = record();
    client["event"] = json!("dst.client.authenticated");
    client["data"] = json!({"userid":"KU_TEST", "steam_id":null, "name": "玩家", "admin":false, "moderator":null});
    assert_eq!(
        events::parse(client.to_string()).unwrap().into_value(),
        client
    );
    for (field, invalid) in [
        ("name", json!("名".repeat(257))),
        ("name", json!(42)),
        ("admin", json!("false")),
        ("moderator", json!(0)),
    ] {
        let mut value = client.clone();
        value["data"][field] = invalid;
        assert!(events::validate(&value).is_err(), "{value}");
    }
    let player = json!({"prefab":"wilson", "guid":42, "userid":"KU_TEST", "position":null});
    for (event, data) in [
        (
            "dst.player.loaded",
            json!({"player":player, "steam_id":null, "name":null, "is_ghost":true, "age_seconds":0}),
        ),
        (
            "dst.player.revived",
            json!({"player":player, "reviver":null, "corpse":false, "method":"ghost", "age_seconds":0}),
        ),
        (
            "dst.entity.death",
            json!({"victim":player, "afflicter":null, "attributed_player":null, "cause":null, "corpsing":false, "age_seconds":0}),
        ),
    ] {
        let mut value = record();
        value["event"] = json!(event);
        value["data"] = data;
        for age in [Value::Null, json!(0), json!(4800.25)] {
            value["data"]["age_seconds"] = age;
            assert_eq!(
                events::parse(value.to_string()).unwrap().into_value(),
                value
            );
        }
        for age in [
            json!(-1),
            json!("4800"),
            json!(true),
            serde_json::from_str("1e400").unwrap(),
        ] {
            value["data"]["age_seconds"] = age;
            assert!(events::validate(&value).is_err(), "{value}");
        }
    }
    let mut equipped = record();
    equipped["event"] = json!("dst.player.equipped");
    equipped["data"] = json!({"player":player,"slot":"hands","item": {
        "prefab":"spear","guid":43,"skin":null,"stack_size":1,
        "condition":{"uses_percent":0.25,"fuel_percent":0}
    }});
    assert_eq!(
        events::parse(equipped.to_string()).unwrap().into_value(),
        equipped
    );
    for condition in [
        Value::Null,
        json!({"uses_percent":null}),
        json!({"uses_percent":"0.25"}),
        json!({"unknown":1}),
    ] {
        let mut value = equipped.clone();
        value["data"]["item"]["condition"] = condition;
        assert!(events::validate(&value).is_err(), "{value}");
    }
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
fn diagnostics_are_sanitized_and_bounded() {
    let mut diagnostic = record();
    diagnostic["event"] = json!("dst.telemetry.error");
    diagnostic["data"] =
        json!({"stage": "player.finishedwork", "message": "callback_failed", "count": 1});
    events::validate(&diagnostic).unwrap();
    assert_eq!(
        events::parse(diagnostic.to_string()).unwrap().into_value(),
        diagnostic
    );
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
    assert!(events::parse(" ".repeat(events::MAX_EVENT_BYTES + 1)).is_err());
    let padded = format!(
        "{:width$}",
        record().to_string(),
        width = events::MAX_EVENT_BYTES
    );
    assert_eq!(events::parse(&padded).unwrap().into_value(), record());
    assert!(events::parse(&(padded + " ")).is_err());
    let mut large = record();
    large["data"] = json!({"name": "season", "value": "x".repeat(events::MAX_EVENT_BYTES)});
    assert!(events::validate(&large).is_err());
    let mut nonfinite = record();
    nonfinite["data"]["value"] = serde_json::from_str("1e400").unwrap();
    assert!(events::validate(&nonfinite).is_err());
    assert!(events::parse(nonfinite.to_string()).is_err());
}
