use dst_server::model::{
    Countdown, EntityRef, Envelope, Error, ErrorCode, MAX_SAFE_INTEGER, OperationHistory,
    OperationStatus, Outcome, PlayerLocation, PlayerState, Readiness, Request, Scope,
    SessionIdentity, ShardResult, Target, describe,
};
use serde_json::{Value, json};

fn call(target: Target, method: &str, arguments: Value) -> dst_server::model::Result<Envelope> {
    Envelope::from_json(
        &serde_json::to_vec(&json!({
            "target": target, "request": { "method": method, "arguments": arguments }
        }))
        .unwrap(),
    )
}

#[test]
fn completion_wait_includes_requested_announcements_and_countdown() {
    let repeated = call(
        Target::Room,
        "announce",
        json!({"message":"notice","count":10,"interval":30}),
    )
    .unwrap();
    assert_eq!(repeated.timeout().unwrap().as_secs(), 120 + 270);
    let stopped = call(
        Target::Room,
        "stop",
        json!({"notice":{"template":"notice","delay":300,"interval":30,"parameters":{}}}),
    )
    .unwrap();
    assert_eq!(stopped.timeout().unwrap().as_secs(), 160 + 300);
    let mut short_wait = repeated.clone();
    short_wait.timeout = Some(1.0);
    assert_eq!(short_wait.timeout().unwrap().as_secs(), 1);
    assert_eq!(
        short_wait.request.completion_timeout().unwrap().as_secs(),
        390
    );
    assert!(
        call(
            Target::Room,
            "announce",
            json!({"message":"notice","count":MAX_SAFE_INTEGER,"interval":1e18})
        )
        .is_err()
    );
}

#[test]
fn deadlines_must_fit_the_monotonic_clock_before_execution() {
    let seconds = 1e19;
    let duration = std::time::Duration::from_secs_f64(seconds);
    assert!(std::time::Instant::now().checked_add(duration).is_none());
    let mut request = call(Target::Room, "status", json!({})).unwrap();
    request.timeout = Some(seconds);
    assert!(request.validate().is_err());
    let notice = Countdown {
        template: "closing".into(),
        delay: seconds,
        interval: 30.0,
        parameters: Default::default(),
    };
    assert!(notice.validate().is_err());
    assert!(call(Target::Room, "stop", json!({"notice":notice})).is_err());
    assert!(dst_server::host_operations::duration(seconds).is_err());
    assert!(dst_server::host_operations::duration(f64::MIN_POSITIVE).is_err());
}

#[test]
fn cli_rejects_duplicate_configuration_keys_before_discarding_them() {
    let directory = tempfile::tempdir().unwrap();
    let root = directory.path().to_str().unwrap();
    let targets = directory.path().join("targets.json");
    std::fs::write(&targets, r#"{"Master":{},"Master":{}}"#).unwrap();
    let cases: &[&[&str]] = &[
        &[
            "config",
            "validate",
            "ClusterSettings",
            r#"{"max_players":1,"max_players":2}"#,
        ],
        &[
            "host",
            "--root",
            root,
            "edit",
            "--all",
            "--set",
            r#"/policy={"mod_auto_update":false,"mod_auto_update":true}"#,
        ],
        &[
            "probe-recovery",
            "--cluster",
            root,
            "--targets",
            targets.to_str().unwrap(),
        ],
    ];
    for arguments in cases {
        let result = std::process::Command::new(env!("CARGO_BIN_EXE_dst-server"))
            .args(*arguments)
            .output()
            .unwrap();
        let error = String::from_utf8_lossy(&result.stderr);
        assert!(!result.status.success(), "{arguments:?}");
        assert!(
            error.contains("invalid request JSON"),
            "{arguments:?}: {error}"
        );
    }
}

#[test]
fn public_contract_validates_scope_defaults_and_nullable_notices() {
    let start = call(Target::Room, "start", json!({})).unwrap();
    assert_eq!(start.timeout().unwrap().as_secs(), 900);
    assert_eq!(
        call(Target::Room, "save", json!({}))
            .unwrap()
            .timeout()
            .unwrap()
            .as_secs(),
        300
    );
    let default_stop = call(Target::Room, "stop", json!({})).unwrap();
    let Request::Stop {
        notice: Some(notice),
    } = default_stop.request
    else {
        panic!()
    };
    assert_eq!((notice.delay, notice.interval), (60.0, 30.0));
    let no_notice = call(Target::Room, "stop", json!({"notice": null})).unwrap();
    assert_eq!(no_notice.request, Request::Stop { notice: None });
    assert_eq!(
        call(Target::Shard("洞穴".into()), "save", json!({}))
            .unwrap_err()
            .code,
        ErrorCode::Unsupported
    );
    assert!(
        call(
            Target::Room,
            "give",
            json!({"userid":"local_player", "item":"twigs"})
        )
        .is_ok()
    );
    for invalid in ["drain", "activate", "typo"] {
        assert!(call(Target::Room, invalid, json!({})).is_err());
    }
    for shard in ["..", "mods", "../World", "World/Two", ""] {
        assert!(call(Target::Shard(shard.into()), "status", json!({})).is_err());
    }
    for duration in [
        f64::NAN,
        f64::INFINITY,
        -1.0,
        0.0,
        f64::MAX,
        f64::MIN_POSITIVE,
    ] {
        let mut value = start.clone();
        value.timeout = Some(duration);
        assert!(value.validate().is_err());
    }
}

#[test]
fn invalid_calls_fail_before_mutation_and_do_not_echo_inputs() {
    for (method, arguments) in [
        ("give", json!({"userid":"", "item":"twigs"})),
        ("give", json!({"userid":"p", "item":"twigs", "count":65})),
        ("give", json!({"userid":"p", "item":"twigs", "count":true})),
        (
            "remove",
            json!({"userid":"p", "item":"twigs", "count":MAX_SAFE_INTEGER+1}),
        ),
        ("set_vitals", json!({"userid":"p", "health":null})),
        ("set_vitals", json!({"userid":"p", "health":1.01})),
        ("ban", json!({"userid":"p", "seconds":0})),
        ("list_snapshots", json!({"limit":101})),
        ("rollback_to_day", json!({"day":0})),
        ("regenerate", json!({"expected_session_id":""})),
        ("start", json!({"unexpected":true})),
        (
            "give",
            json!({"userid":"p", "item":"twigs", "count":"sensitive-value"}),
        ),
    ] {
        let error = call(Target::Room, method, arguments).unwrap_err();
        assert_eq!(error.code, ErrorCode::Invalid, "{method}");
        assert!(!error.message.contains("sensitive-value"));
    }
    let valid = call(
        Target::Room,
        "set_vitals",
        json!({"userid":"p", "health":0.0, "temperature":-10.5}),
    )
    .unwrap();
    assert_eq!(valid.request.arguments().unwrap()["health"], 0.0);
    assert!(
        Envelope::new(
            Target::Room,
            Request::Teleport {
                userid: "p".into(),
                x: f64::NAN,
                y: 0.0,
                z: 0.0
            }
        )
        .is_err()
    );
    assert!(
        call(
            Target::Shard("World".into()),
            "evaluate",
            json!({"source":"x".repeat(4097)})
        )
        .is_err()
    );
    assert!(
        call(
            Target::Shard("World".into()),
            "evaluate",
            json!({"source":"print('世界')\nreturn 1"})
        )
        .is_ok()
    );

    for input in [
        r#"{"target":{"scope":"room"},"request":{"method":"start","method":"stop","arguments":{}}}"#,
        r#"{"target":{"scope":"room"},"request":{"method":"stop","arguments":{"notice":{"template":"{x}","parameters":{"x":1,"x":2}}}}}"#,
        r#"{"target":{"scope":"room"},"request":{"method":"start","arguments":{}},"timeout":NaN}"#,
        r#"{"target":{"scope":"room"},"request":{"method":"stop","arguments":{"notice":{"template":"{x}","parameters":{"x":1e999}}}}}"#,
    ] {
        assert!(Envelope::from_json(input.as_bytes()).is_err());
    }
}

#[test]
fn registry_is_the_discovery_contract_for_every_method() {
    let mut seen = std::collections::HashSet::new();
    for method in dst_server::model::METHODS {
        assert!(seen.insert(method.name));
        let description = method.description();
        let mut arguments = serde_json::Map::new();
        for name in description.arguments_schema["required"].as_array().unwrap() {
            let name = name.as_str().unwrap();
            arguments.insert(
                name.into(),
                match name {
                    "userid" => json!("player"),
                    "source" => json!("return 1"),
                    "message" => json!("hello"),
                    "configuration" | "policy" => json!({}),
                    "artifact_id" => json!("01ARZ3NDEKTSV4RRFFQ69G5FAV"),
                    "paused" => json!(true),
                    "session_id" | "shard_id" => json!("1"),
                    "item" => json!("twigs"),
                    "x" | "y" | "z" => json!(1.0),
                    _ => json!(1),
                },
            );
        }
        if method.name == "set_vitals" {
            arguments.insert("health".into(), json!(1.0));
        }
        let target = if method.scopes.contains(&Scope::Room) {
            Target::Room
        } else {
            Target::Shard("World".into())
        };
        let envelope = call(target, method.name, Value::Object(arguments)).unwrap();
        assert_eq!(envelope.request.method(), method.name);
        assert_eq!(envelope.request.mutating(), method.mutation);
        assert_eq!(
            envelope.timeout().unwrap().as_secs_f64(),
            method.default_timeout
        );
        assert!(
            describe(envelope.target.scope())
                .iter()
                .any(|item| item == &description)
        );
        let serialized = serde_json::to_vec(&envelope).unwrap();
        assert_eq!(Envelope::from_json(&serialized).unwrap(), envelope);
    }
    assert!(seen.contains("rollback_to_snapshot"));
    assert!(
        describe(Scope::Shard)
            .iter()
            .all(|method| method.name != "update_mods")
    );
}

#[test]
fn countdown_templates_reject_execution_and_preserve_literal_braces() {
    let base = Countdown {
        template: "{{literal}} {remaining} {minutes} {when} {name}".into(),
        delay: 0.0,
        interval: 30.0,
        parameters: std::collections::BTreeMap::from([("name".into(), json!("房间"))]),
    };
    assert!(base.validate().is_ok());
    for template in [
        "{name.attribute}",
        "{name[0]}",
        "{name!r}",
        "{name:02}",
        "{missing}",
        "{",
        "}",
    ] {
        assert!(
            Countdown {
                template: template.into(),
                ..base.clone()
            }
            .validate()
            .is_err()
        );
    }
    assert!(
        Countdown {
            parameters: std::collections::BTreeMap::from([("when".into(), json!("override"))]),
            ..base.clone()
        }
        .validate()
        .is_err()
    );
    assert!(
        Countdown {
            interval: 0.0,
            ..base
        }
        .validate()
        .is_err()
    );
}

#[test]
fn world_identity_readiness_and_operation_results_are_unambiguous() {
    let identity = SessionIdentity {
        session_id: "world".into(),
        process_id: "first-process".into(),
        generation: 0,
    };
    let entity = EntityRef {
        shard: "World".into(),
        identity: identity.clone(),
        guid: 123,
    };
    assert!(entity.validate_current("World", &identity).is_ok());
    let restarted = SessionIdentity {
        process_id: "next-process".into(),
        ..identity.clone()
    };
    assert_eq!(
        entity
            .validate_current("World", &restarted)
            .unwrap_err()
            .code,
        ErrorCode::StaleReference
    );
    let mut readiness = Readiness::default();
    assert_eq!(
        readiness.require("World").unwrap_err().message,
        "game process is stopped"
    );
    readiness.process_running = true;
    assert_eq!(
        readiness.require("World").unwrap_err().message,
        "world is still loading"
    );
    readiness.world_loaded = true;
    assert_eq!(
        readiness.require("World").unwrap_err().message,
        "required game control is unavailable"
    );
    readiness.control_ready = true;
    readiness.missing_shards.push("Cave".into());
    assert!(!readiness.ready());
    assert_eq!(
        readiness.require("World").unwrap_err().details["readiness"]["missing_shards"],
        json!(["Cave"])
    );
    readiness.missing_shards.clear();
    assert!(readiness.require("World").is_ok());

    let mut player = PlayerLocation {
        userid: "player".into(),
        shard: Some("World".into()),
        identity: Some(identity),
        state: PlayerState::Migrating,
        player: Value::Null,
    };
    assert_eq!(
        player.require_active().unwrap_err().code,
        ErrorCode::NotReady
    );
    player.state = PlayerState::Conflict;
    assert_eq!(
        player.require_active().unwrap_err().code,
        ErrorCode::Conflict
    );
    player.state = PlayerState::Active;
    assert_eq!(player.require_active().unwrap().0, "World");

    let results: Vec<ShardResult<Value>> = vec![
        ShardResult {
            shard: "World".into(),
            result: Outcome::Success { value: Value::Null },
        },
        ShardResult {
            shard: "Cave".into(),
            result: Outcome::Failure {
                error: Error::new(ErrorCode::Unknown, "save confirmation was lost"),
            },
        },
    ];
    let history = OperationHistory {
        current: None,
        last: Some(OperationStatus {
            id: "operation-id".into(),
            method: "save".into(),
            target: Target::Room,
            started_at_ns: 1_700_000_000_000_000_001,
            completed_at_ns: Some(1_700_000_000_000_000_010),
            result: Some(Outcome::Failure {
                error: Error::new(
                    ErrorCode::PartialFailure,
                    "not all shard saves were confirmed",
                )
                .with_details(json!(results)),
            }),
        }),
    };
    let encoded = serde_json::to_vec(&history).unwrap();
    assert_eq!(
        serde_json::from_slice::<OperationHistory>(&encoded).unwrap(),
        history
    );
    let partial = &serde_json::to_value(history).unwrap()["last"]["result"]["error"]["details"];
    assert_eq!(
        partial[0]["result"],
        json!({"status":"success", "value":null})
    );
    assert_eq!(partial[1]["result"]["error"]["code"], "unknown");
}
