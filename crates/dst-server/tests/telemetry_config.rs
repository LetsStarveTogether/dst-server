use std::{collections::BTreeMap, time::Duration};

use dst_server::{
    driver::{DriverEvent, DriverEventKind},
    telemetry::{LogEvent, TelemetryConfig},
};
use opentelemetry_proto::tonic::common::v1::any_value::Value as Proto;
use serde_json::json;

fn configuration(values: &[(&str, &str)]) -> anyhow::Result<TelemetryConfig> {
    let values: BTreeMap<_, _> = values.iter().copied().collect();
    TelemetryConfig::from_lookup(|name| values.get(name).map(|value| (*value).to_owned()))
}

#[test]
fn log_environment_precedence_preserves_encoded_headers_and_resource_types() -> anyhow::Result<()> {
    let config = configuration(&[
        ("OTEL_EXPORTER_OTLP_ENDPOINT", "http://global.test:4317"),
        ("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "https://logs.test:4317"),
        ("OTEL_EXPORTER_OTLP_LOGS_INSECURE", "true"),
        (
            "OTEL_EXPORTER_OTLP_HEADERS",
            "authorization=unused-global-secret",
        ),
        (
            "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
            "authorization=Bearer%20fixture-token,x-plus=a+b%2Bc,x-comma=a%2Cb",
        ),
        ("OTEL_EXPORTER_OTLP_COMPRESSION", "none"),
        ("OTEL_EXPORTER_OTLP_LOGS_COMPRESSION", "GZIP"),
        ("OTEL_EXPORTER_OTLP_TIMEOUT", "20000"),
        ("OTEL_EXPORTER_OTLP_LOGS_TIMEOUT", "1500"),
        ("OTEL_BLRP_EXPORT_TIMEOUT", "500"),
        ("OTEL_BLRP_SCHEDULE_DELAY", "250"),
        ("OTEL_BLRP_MAX_QUEUE_SIZE", "8"),
        ("OTEL_BLRP_MAX_EXPORT_BATCH_SIZE", "2"),
        (
            "OTEL_RESOURCE_ATTRIBUTES",
            "service.name=generic,team=a%2Cb,enabled=false,empty=",
        ),
        ("OTEL_SERVICE_NAME", "dst-agent"),
    ])?;
    let logs = config.logs.unwrap();
    assert_eq!(logs.endpoint, "https://logs.test:4317");
    assert!(logs.tls.is_some());
    assert!(logs.gzip);
    assert_eq!(logs.export_timeout, Duration::from_millis(500));
    assert_eq!(logs.schedule_delay, Duration::from_millis(250));
    assert_eq!(logs.queue_capacity, 8);
    assert_eq!(logs.max_batch_records, 2);
    assert_eq!(
        logs.headers.get("authorization").unwrap(),
        "Bearer fixture-token"
    );
    assert_eq!(logs.headers.get("x-plus").unwrap(), "a+b+c");
    assert_eq!(logs.headers.get("x-comma").unwrap(), "a,b");
    assert!(logs.headers.get("authorization").unwrap().is_sensitive());
    assert!(!format!("{:?}", logs.headers).contains("fixture-token"));
    assert_eq!(config.resource["service.name"], "dst-agent");
    assert_eq!(config.resource["team"], "a,b");
    assert_eq!(config.resource["enabled"], "false");
    assert_eq!(config.resource["empty"], "");
    assert!(
        config.resource["service.instance.id"]
            .as_str()
            .unwrap()
            .parse::<ulid::Ulid>()
            .is_ok()
    );
    Ok(())
}

#[test]
fn disabled_logs_do_not_read_credentials_and_empty_values_are_unset() -> anyhow::Result<()> {
    for switch in ["OTEL_SDK_DISABLED", "OTEL_LOGS_EXPORTER"] {
        let config = configuration(&[
            (
                switch,
                if switch == "OTEL_SDK_DISABLED" {
                    "TRUE"
                } else {
                    "NONE"
                },
            ),
            ("OTEL_EXPORTER_OTLP_LOGS_PROTOCOL", "http/protobuf"),
            (
                "OTEL_EXPORTER_OTLP_LOGS_CERTIFICATE",
                "/missing/private-path",
            ),
        ])?;
        assert!(config.logs.is_none());
    }
    let config = configuration(&[
        ("OTEL_LOGS_EXPORTER", ""),
        ("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", ""),
        ("OTEL_EXPORTER_OTLP_ENDPOINT", "collector.test:4317"),
        ("OTEL_EXPORTER_OTLP_LOGS_INSECURE", "TrUe"),
        ("OTEL_RESOURCE_ATTRIBUTES", "service.name=configured"),
        ("OTEL_SERVICE_NAME", ""),
    ])?;
    let logs = config.logs.unwrap();
    assert_eq!(logs.endpoint, "http://collector.test:4317");
    assert!(logs.tls.is_none());
    assert_eq!(config.resource["service.name"], "configured");
    assert_eq!(logs.drain_timeout, Duration::from_secs(30));
    let secure = configuration(&[("OTEL_EXPORTER_OTLP_ENDPOINT", "collector.test:4317")])?;
    assert_eq!(secure.logs.unwrap().endpoint, "https://collector.test:4317");
    Ok(())
}

#[test]
fn invalid_transport_configuration_cannot_expose_credentials() {
    for values in [
        vec![(
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "https://fixture-secret:secret@collector.test",
        )],
        vec![(
            "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
            "authorization=fixture-secret%0Ainjected",
        )],
        vec![
            ("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.test"),
            ("OTEL_EXPORTER_OTLP_CLIENT_KEY", "/fixture-secret/key"),
        ],
        vec![
            ("OTEL_EXPORTER_OTLP_ENDPOINT", "https://collector.test"),
            ("OTEL_EXPORTER_OTLP_CERTIFICATE", "/fixture-secret/absent"),
        ],
        vec![("OTEL_BLRP_MAX_QUEUE_SIZE", "1")],
        vec![("OTEL_EXPORTER_OTLP_TIMEOUT", "0")],
        vec![("OTEL_EXPORTER_OTLP_LOGS_PROTOCOL", "http/protobuf")],
    ] {
        let error = configuration(&values)
            .err()
            .expect("invalid configuration accepted");
        assert!(!format!("{error:#}").contains("fixture-secret"));
    }
}

fn telemetry_event() -> DriverEvent {
    DriverEvent {
        kind: DriverEventKind::Telemetry,
        shard: "Master".into(),
        nonce: "attempt-fixture".into(),
        observed_timestamp_ns: 1_700_000_000_123_456_789,
        generation: Some(7),
        data: json!({
            "v":3,"nonce":"attempt-fixture","generation":7,"seq":9007199254740993_u64,
            "event":"dst.player.action","tick":9876,"monotonic_ms":123456,
            "session_id":"","cycle":0,
            "data":{"nested":[null,false,0,"",{"exact":9007199254740993_i64}],"success":false}
        }),
    }
}

#[test]
fn driver_event_identity_body_and_source_time_survive_both_sinks() -> anyhow::Result<()> {
    let source = telemetry_event();
    let log = LogEvent::from_driver(&source, "cluster")?;
    assert_eq!(log.event_name, "dst.player.action");
    assert_eq!(log.body, source.data["data"]);
    assert_eq!(log.observed_timestamp_ns, source.observed_timestamp_ns);
    assert_eq!(
        log.attributes["log.record.uid"],
        "attempt-fixture:7:9007199254740993"
    );
    assert_eq!(log.attributes["dst.cluster.name"], "cluster");
    assert_eq!(log.attributes["dst.shard.name"], "Master");
    assert_eq!(log.attributes["dst.session.id"], "");
    assert_eq!(log.attributes["dst.world.cycle"], 0);
    assert_eq!(log.attributes["dst.event.sequence"], 9007199254740993_u64);
    let local = serde_json::to_value(&log)?;
    let exported = log.to_otlp()?;
    assert_eq!(exported.time_unix_nano, source.observed_timestamp_ns);
    assert_eq!(
        exported.observed_time_unix_nano,
        source.observed_timestamp_ns
    );
    let uid = exported
        .attributes
        .iter()
        .find(|attribute| attribute.key == "log.record.uid")
        .unwrap()
        .value
        .as_ref()
        .unwrap();
    assert_eq!(
        uid.value,
        Some(Proto::StringValue(
            local["attributes"]["log.record.uid"]
                .as_str()
                .unwrap()
                .into()
        ))
    );
    assert_eq!(
        LogEvent::from_driver(&source, "cluster")?.attributes["log.record.uid"],
        log.attributes["log.record.uid"]
    );
    let mut stale = source;
    stale.generation = Some(8);
    assert!(LogEvent::from_driver(&stale, "cluster").is_err());
    Ok(())
}

#[test]
fn operational_observations_keep_one_unique_id_per_record() -> anyhow::Result<()> {
    let mut source = telemetry_event();
    source.kind = DriverEventKind::Lifecycle;
    source.generation = None;
    source.data = json!({"event":"ready","success":false,"empty":null});
    let log = LogEvent::from_driver(&source, "cluster")?;
    assert_eq!(log.event_name, "dst.server.ready");
    assert_eq!(log.body, source.data);
    assert!(!log.attributes.contains_key("dst.runtime.generation"));
    assert!(
        log.attributes["log.record.uid"]
            .as_str()
            .unwrap()
            .parse::<ulid::Ulid>()
            .is_ok()
    );
    assert_eq!(
        serde_json::to_value(log.clone())?,
        serde_json::to_value(&log)?
    );
    assert_ne!(
        log.attributes["log.record.uid"],
        LogEvent::from_driver(&source, "cluster")?.attributes["log.record.uid"]
    );
    source.kind = DriverEventKind::Control;
    assert_eq!(
        LogEvent::from_driver(&source, "cluster")?.event_name,
        "dst.control.ready"
    );
    source.kind = DriverEventKind::Log;
    source.data = json!({"stream":"stderr", "line":"example\n"});
    assert_eq!(
        LogEvent::from_driver(&source, "cluster")?.severity_text,
        "WARN"
    );
    source.kind = DriverEventKind::Diagnostic;
    assert_eq!(
        LogEvent::from_driver(&source, "cluster")?.severity_text,
        "ERROR"
    );
    Ok(())
}
