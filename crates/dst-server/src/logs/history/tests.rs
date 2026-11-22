use super::*;

const UID: &str = "01ARZ3NDEKTSV4RRFFQ69G5FAV";

#[test]
fn old_chat_without_a_sequence_is_counted_only_in_the_gameplay_scan() {
    let message = r#"DST_RECORD|{"event_name":"dst.player.chat","attributes":{},"body":{"userid":"KU_a","message":"old text"}}"#;
    assert!(
        regex::Regex::new(candidate_pattern(false))
            .unwrap()
            .is_match(message)
    );
    assert!(matches!(
        decode_message(message, &Default::default(), false),
        Err(Rejection::UnsupportedSchema)
    ));
    assert!(
        !regex::Regex::new(candidate_pattern(true))
            .unwrap()
            .is_match(message)
    );
    assert!(
        decode_message(message, &Default::default(), true)
            .unwrap()
            .is_none()
    );
}

fn meal() -> Value {
    json!({
        "event_name":"dst.player.ate", "observed_timestamp_ns":0,
        "attributes":{
            "log.record.uid":UID, "dst.cluster.name":"001", "dst.shard.name":"Forest",
            "dst.game.attempt.id":UID, "dst.runtime.generation":0, "dst.event.sequence":1,
            "dst.tick":0, "dst.monotonic_ms":0
        },
        "body":{"player":{"guid":1, "userid":"KU_eater", "prefab":"wilson"}, "feeder":null, "food":"carrot"}
    })
}

#[test]
fn duplicate_json_members_cannot_change_a_journal_observation() {
    let canonical = format!("{PREFIX}{}", meal());
    for (key, duplicate) in [
        (
            r#""event_name":"#,
            r#""event_name":"dst.player.chat","event_name":"#,
        ),
        (
            r#""log.record.uid":"#,
            r#""log.record.uid":"invalid","log.record.uid":"#,
        ),
        (r#""userid":"#, r#""user\u0069d":"other","userid":"#),
    ] {
        let ambiguous = canonical.replacen(key, duplicate, 1);
        assert!(matches!(
            decode_message(&ambiguous, &Default::default(), false),
            Err(Rejection::InvalidEnvelope)
        ));
    }
}

#[test]
fn former_meals_require_valid_actor_item_and_sequence() {
    let decode = |log| decode_record(log, &EventFilter::default(), false);
    let mut prior = meal();
    prior["body"]["player"]["position"] = Value::Null;
    prior["body"]["food"] = json!({"guid":2, "prefab":"carrot", "skin":null, "stack_size":1,
        "condition":{"freshness_percent":0.7}});
    assert!(matches!(
        decode(prior.clone()),
        Err(Rejection::UnsupportedSchema)
    ));
    for (pointer, corrupt) in [
        ("/body/player", Value::Null),
        ("/body/player/position", json!([])),
        ("/body/food/guid", json!(0)),
        ("/body/food/condition", json!({"unknown":1})),
        ("/attributes/dst.event.sequence", json!(0)),
    ] {
        let mut broken = prior.clone();
        *broken.pointer_mut(pointer).unwrap() = corrupt;
        assert!(
            matches!(decode(broken), Err(Rejection::InvalidGameplay)),
            "{pointer}"
        );
    }
    prior["body"].as_object_mut().unwrap().remove("feeder");
    assert!(matches!(decode(prior), Err(Rejection::InvalidGameplay)));
}

#[test]
fn optional_log_headers_are_checked_when_present() {
    let decode = |log| decode_record(log, &EventFilter::default(), false);
    let legacy = decode(meal()).unwrap().unwrap();
    assert!(legacy.severity_text.is_none() && legacy.trace.is_none());
    let mut log = meal();
    log["severity_text"] = json!("info");
    log["trace_id"] = json!("a".repeat(32));
    log["span_id"] = json!("b".repeat(16));
    log["trace_flags"] = json!(1);
    assert!(decode(log.clone()).unwrap().unwrap().trace.is_some());
    for (field, value) in [
        ("trace_id", json!("0".repeat(32))),
        ("span_id", json!("not-hex")),
        ("span_id", json!("42")),
        ("trace_flags", json!(256)),
        ("severity_text", json!("verbose")),
    ] {
        let mut broken = log.clone();
        broken[field] = value;
        assert!(
            matches!(decode(broken), Err(Rejection::InvalidEnvelope)),
            "{field}"
        );
    }
    log.as_object_mut().unwrap().remove("trace_flags");
    assert!(matches!(decode(log), Err(Rejection::InvalidEnvelope)));
}

#[test]
fn participant_filter_accepts_the_same_literal_ids_as_the_game() {
    let mut log = meal();
    let userid = "甲\t.\0";
    log["body"]["player"]["userid"] = json!(userid);
    let mut filter = EventFilter {
        userid: Some(userid.into()),
        ..Default::default()
    };
    filter.validate(false).unwrap();
    assert!(decode_record(log, &filter, false).unwrap().is_some());
    filter.userid = Some("界".repeat(129));
    assert!(filter.validate(false).is_err());
}

#[test]
fn lifecycle_metadata_and_filtered_gameplay_are_distinct() {
    let mut log = meal();
    log["attributes"]
        .as_object_mut()
        .unwrap()
        .remove("dst.event.sequence");
    for name in [
        "dst.recovery.transition",
        "dst.admin.operation",
        "dst.policy.decision",
        "dst.mod.maintenance",
    ] {
        log["event_name"] = json!(name);
        assert!(live_auxiliary(&log, true).unwrap());
    }
    log["event_name"] = json!("dst.player.chat");
    assert!(matches!(
        live_auxiliary(&log, true),
        Err(Rejection::UnsupportedSchema)
    ));
    log["event_name"] = json!("dst.unknown");
    log["attributes"]["dst.event.sequence"] = json!(2);
    assert!(!live_auxiliary(&log, true).unwrap());
    assert!(matches!(
        decode_record(log, &EventFilter::default(), false),
        Err(Rejection::UnsupportedSchema)
    ));
    let filter = EventFilter {
        userid: Some("other".into()),
        ..Default::default()
    };
    assert!(!live_auxiliary(&meal(), false).unwrap());
    assert!(decode_record(meal(), &filter, false).unwrap().is_none());
}
