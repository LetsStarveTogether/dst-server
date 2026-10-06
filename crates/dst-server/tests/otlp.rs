use std::{sync::Arc, time::Duration};

use dst_server::telemetry::{
    ExportConfig, LogEvent, LogExporter, Submission, any_value, export_request,
};
use opentelemetry_proto::tonic::{
    collector::logs::v1::{
        ExportLogsPartialSuccess, ExportLogsServiceRequest, ExportLogsServiceResponse,
        logs_service_client::LogsServiceClient,
    },
    common::v1::{AnyValue, KeyValue, any_value::Value as Proto},
    logs::v1::SeverityNumber,
};
use prost::Message;
use serde_json::{Value, json};
use tokio::sync::Semaphore;
use tonic::{Code, Status};
use tonic_types::{ErrorDetails, StatusExt};

#[path = "fixtures/large_events.rs"]
mod large_events;
#[path = "fixtures/log_collector.rs"]
mod log_collector;
#[path = "fixtures/stopped_room.rs"]
mod stopped_room;
use log_collector::Collector;

fn attribute<'a>(values: &'a [KeyValue], key: &str) -> &'a AnyValue {
    values
        .iter()
        .find(|pair| pair.key == key)
        .unwrap()
        .value
        .as_ref()
        .unwrap()
}

#[tokio::test]
async fn structured_event_survives_protobuf_and_real_grpc_export() -> anyhow::Result<()> {
    tokio::time::timeout(Duration::from_secs(10), async {
        let event = LogEvent {
            trace: None,
            event_name: "dst.test.value_types".into(),
            body: json!({
                "null": null,
                "zero": 0,
                "empty": "",
                "false": false,
                "float": 1.0,
                "large_integer": 9_007_199_254_740_993_i64,
                "min_integer": i64::MIN,
                "max_integer": i64::MAX,
                "nested": {"list": [null, 0, "", false, 0.5, {}, []]},
                "userid": "18446744073709551616",
                "steam_id": "76561198000000001",
                "name": "玩家改名后",
                "admin": false,
                "age_seconds": 4800.25
            }),
            observed_timestamp_ns: 1_791_013_234_567_890_123,
            severity_text: "info".into(),
            attributes: json!({"log.record.uid": ulid::Ulid::generate().to_string(), "optional": null})
                .as_object().unwrap().clone(),
        };
        let resource = json!({"service.name": "dst-server", "dst.cluster.name": "房间"});
        let expected = export_request(resource.as_object().unwrap(), std::slice::from_ref(&event))?;
        let decoded = ExportLogsServiceRequest::decode(expected.encode_to_vec().as_slice())?;
        assert_eq!(decoded, expected);

        let mut collector = Collector::start(vec![], None).await?;
        let mut client = LogsServiceClient::connect(collector.endpoint.clone()).await?;
        let response = client.export(expected.clone()).await?.into_inner();
        assert!(response.partial_success.is_none());
        let received = collector.receive().await?.message;
        assert_eq!(received, expected);
        collector.stop().await?;

        let resource_logs = &received.resource_logs[0];
        assert_eq!(
            attribute(&resource_logs.resource.as_ref().unwrap().attributes, "dst.cluster.name").value,
            Some(Proto::StringValue("房间".into()))
        );
        let record = &resource_logs.scope_logs[0].log_records[0];
        assert_eq!(record.time_unix_nano, event.observed_timestamp_ns);
        assert_eq!(record.observed_time_unix_nano, event.observed_timestamp_ns);
        assert_eq!(record.event_name, event.event_name);
        assert_eq!(record.severity_number, SeverityNumber::Info as i32);
        assert_eq!(record.severity_text, "INFO");
        assert_eq!(attribute(&record.attributes, "optional").value, None);
        let Some(Proto::KvlistValue(body)) = &record.body.as_ref().unwrap().value else {
            panic!("body must remain a structured object");
        };
        for (key, expected) in [
            ("null", None),
            ("zero", Some(Proto::IntValue(0))),
            ("empty", Some(Proto::StringValue(String::new()))),
            ("steam_id", Some(Proto::StringValue("76561198000000001".into()))),
            ("name", Some(Proto::StringValue("玩家改名后".into()))),
            ("admin", Some(Proto::BoolValue(false))),
            ("age_seconds", Some(Proto::DoubleValue(4800.25))),
            ("false", Some(Proto::BoolValue(false))),
            ("float", Some(Proto::DoubleValue(1.0))),
            ("large_integer", Some(Proto::IntValue(9_007_199_254_740_993))),
            ("min_integer", Some(Proto::IntValue(i64::MIN))),
            ("max_integer", Some(Proto::IntValue(i64::MAX))),
        ] {
            assert_eq!(attribute(&body.values, key).value, expected, "{key}");
        }
        let Some(Proto::KvlistValue(nested)) = &attribute(&body.values, "nested").value else {
            panic!("nested object lost");
        };
        let Some(Proto::ArrayValue(list)) = &attribute(&nested.values, "list").value else {
            panic!("nested list lost");
        };
        assert_eq!(list.values.len(), 7);
        assert_eq!(list.values[0].value, None);
        assert!(matches!(&list.values[5].value, Some(Proto::KvlistValue(map)) if map.values.is_empty()));
        assert!(matches!(&list.values[6].value, Some(Proto::ArrayValue(array)) if array.values.is_empty()));
        Ok::<_, anyhow::Error>(())
    }).await?
}

#[test]
fn invalid_values_fail_before_export_without_rounding() -> anyhow::Result<()> {
    for input in [
        "9223372036854775808",
        "-9223372036854775809",
        "18446744073709551616",
        "-18446744073709551616",
        "1e999",
    ] {
        let value: Value = serde_json::from_str(input)?;
        assert!(any_value(&value).is_err(), "accepted {input}");
    }
    assert_eq!(
        any_value(&serde_json::from_str("1e20")?)?.value,
        Some(Proto::DoubleValue(1e20))
    );
    let invalid = LogEvent {
        trace: None,
        event_name: "dst.test".into(),
        body: Value::Null,
        observed_timestamp_ns: 0,
        severity_text: "invalid".into(),
        attributes: Default::default(),
    };
    assert!(invalid.to_otlp().is_err());
    let mut deep = Value::Null;
    for _ in 0..24 {
        deep = json!({"key": deep});
    }
    let deepest = LogEvent {
        body: deep.clone(),
        severity_text: "INFO".into(),
        ..invalid.clone()
    };
    let deepest_request = export_request(&Default::default(), &[deepest])?;
    assert_eq!(
        ExportLogsServiceRequest::decode(deepest_request.encode_to_vec().as_slice())?,
        deepest_request
    );
    assert!(any_value(&json!({"key": deep})).is_err());
    let empty_name = LogEvent {
        trace: None,
        event_name: String::new(),
        severity_text: "INFO".into(),
        ..invalid
    };
    assert!(empty_name.to_otlp().is_err());
    Ok(())
}

impl Collector {
    fn config(&self) -> ExportConfig {
        ExportConfig {
            endpoint: self.endpoint.clone(),
            schedule_delay: Duration::from_secs(60),
            export_timeout: Duration::from_secs(2),
            drain_timeout: Duration::from_secs(3),
            ..Default::default()
        }
    }
}

fn event() -> LogEvent {
    LogEvent {
        trace: None,
        event_name: "dst.client.authenticated".into(),
        body: json!({"userid": "KU_test"}),
        observed_timestamp_ns: 1_791_013_234_567_890_123,
        severity_text: "INFO".into(),
        attributes: Default::default(),
    }
}

#[tokio::test]
async fn legal_maximum_game_frames_survive_exporter_and_collector() -> anyhow::Result<()> {
    let mut collector = Collector::start(vec![], None).await?;
    let (resource, cluster, shard) = large_events::dense_context().await;
    let exporter = LogExporter::new(collector.config(), &resource)?;
    let events = large_events::records();
    let count = events.len() as u64;
    let mut largest_local = 0;
    for mut event in events {
        event
            .attributes
            .insert("dst.cluster.name".into(), json!(cluster));
        event
            .attributes
            .insert("dst.shard.name".into(), json!(shard));
        let expected = export_request(&resource, std::slice::from_ref(&event))?;
        largest_local =
            largest_local.max(serde_json::to_vec(&large_events::local_record(&event))?.len());
        assert_eq!(
            exporter.submit(&event)?,
            Submission::Accepted,
            "{}",
            event.event_name
        );
        exporter.flush().await?;
        let received = collector.receive().await?.message;
        let record = &received.resource_logs[0].scope_logs[0].log_records[0];
        assert_eq!(record.time_unix_nano, 0);
        assert_eq!(record.observed_time_unix_nano, event.observed_timestamp_ns);
        assert!(received == expected, "{} changed", event.event_name);
    }
    assert!(largest_local > 64 * 1024);
    let stats = exporter.close().await?;
    assert_eq!((stats.exported, stats.too_large), (count, 0));
    collector.stop().await
}

#[tokio::test]
async fn admin_operations_export_targets_results_and_failures_while_games_are_stopped()
-> anyhow::Result<()> {
    use dst_server::model::{Envelope, Request, Target};
    let mut collector = Collector::start(vec![], None).await?;
    let (_directory, room) = stopped_room::open(dst_server::telemetry::TelemetryConfig {
        logs: Some(collector.config()),
        ..Default::default()
    })?;
    for (request, succeeded) in [
        (
            Request::Ban {
                userid: "KU_TARGET".into(),
                seconds: None,
            },
            true,
        ),
        (
            Request::Unban {
                userid: "KU_ABSENT".into(),
            },
            true,
        ),
        (
            Request::Ban {
                userid: "KU_TARGET".into(),
                seconds: Some(60),
            },
            false,
        ),
        (
            Request::SetAdmin {
                userid: "KU_ADMIN".into(),
                enabled: true,
            },
            true,
        ),
        (
            Request::Whitelist {
                userid: "KU_GUEST".into(),
            },
            true,
        ),
        (
            Request::Unwhitelist {
                userid: "KU_GUEST".into(),
            },
            true,
        ),
        (
            Request::Kick {
                userid: "KU_ABSENT".into(),
            },
            false,
        ),
    ] {
        assert_eq!(
            room.invoke(Envelope::new(Target::Room, request)?)
                .await
                .is_ok(),
            succeeded
        );
    }
    room.close_telemetry().await?;
    let received = collector.receive().await?.message;
    let all_records = &received.resource_logs[0].scope_logs[0].log_records;
    assert_eq!(
        all_records
            .iter()
            .filter(|record| record.event_name == "dst.server.operation")
            .count(),
        14
    );
    let records: Vec<_> = all_records
        .iter()
        .filter(|record| record.event_name == "dst.admin.operation")
        .collect();
    assert_eq!(records.len(), 7);
    for (record, (method, userid, status, result)) in records.iter().zip([
        ("ban", "KU_TARGET", "success", None),
        ("unban", "KU_ABSENT", "success", None),
        ("ban", "KU_TARGET", "failure", None),
        ("set_admin", "KU_ADMIN", "success", None),
        ("whitelist", "KU_GUEST", "success", None),
        ("unwhitelist", "KU_GUEST", "success", None),
        ("kick", "KU_ABSENT", "failure", None),
    ]) {
        assert_eq!(record.event_name, "dst.admin.operation");
        let Some(Proto::KvlistValue(body)) = &record.body.as_ref().unwrap().value else {
            panic!("structured admin log")
        };
        for (key, value) in [("method", method), ("userid", userid), ("status", status)] {
            assert_eq!(
                attribute(&body.values, key).value,
                Some(Proto::StringValue(value.into()))
            );
        }
        assert_eq!(attribute(&body.values, "result").value, result);
        assert_ne!(
            attribute(&body.values, "operation_id"),
            attribute(&record.attributes, "log.record.uid")
        );
        assert_eq!(
            attribute(&body.values, "operation_id"),
            attribute(&record.attributes, "dst.operation.id")
        );
        assert_eq!(
            attribute(&body.values, "error_code").value,
            (status == "failure").then(|| Proto::StringValue("not_ready".into()))
        );
        if method == "ban" && status == "failure" {
            assert_eq!(
                attribute(&body.values, "seconds").value,
                Some(Proto::IntValue(60))
            );
        }
    }
    collector.stop().await?;
    Ok(())
}

#[tokio::test]
async fn retries_stop_after_partial_acceptance_and_preserve_counts() -> anyhow::Result<()> {
    let mut collector = Collector::start(
        vec![
            Err(Status::unavailable("collector offline")),
            Err(Status::unavailable("collector offline")),
            Ok(ExportLogsServiceResponse {
                partial_success: Some(ExportLogsPartialSuccess {
                    rejected_log_records: 1,
                    error_message: "one rejected".into(),
                }),
            }),
        ],
        None,
    )
    .await?;
    let exporter = LogExporter::new(collector.config(), &Default::default())?;
    assert_eq!(exporter.submit(&event())?, Submission::Accepted);
    assert_eq!(exporter.submit(&event())?, Submission::Accepted);
    let stats = exporter.flush().await?;
    assert_eq!(
        (
            stats.accepted,
            stats.exported,
            stats.rejected,
            stats.retries
        ),
        (2, 1, 1, 2)
    );
    assert_eq!((stats.pending_records, stats.pending_bytes), (0, 0));
    let first = collector.receive().await?;
    for _ in 0..2 {
        assert_eq!(collector.receive().await?.message, first.message);
    }
    let stats = exporter.close().await?;
    assert_eq!((stats.exported, stats.rejected, stats.failed), (1, 1, 0));
    assert!(collector.received.try_recv().is_err());
    collector.stop().await
}

#[tokio::test]
async fn permanent_rejection_does_not_retry() -> anyhow::Result<()> {
    for status in [
        Status::permission_denied("denied"),
        Status::resource_exhausted("message too large"),
    ] {
        let mut collector = Collector::start(vec![Err(status)], None).await?;
        let exporter = LogExporter::new(collector.config(), &Default::default())?;
        exporter.submit(&event())?;
        let stats = exporter.close().await?;
        assert_eq!((stats.failed, stats.retries, stats.exported), (1, 0, 0));
        collector.receive().await?;
        assert!(collector.received.try_recv().is_err());
        collector.stop().await?;
    }
    Ok(())
}

#[tokio::test]
async fn retry_info_headers_and_gzip_reach_the_collector() -> anyhow::Result<()> {
    let status = Status::with_error_details(
        Code::ResourceExhausted,
        "throttled",
        ErrorDetails::with_retry_info(Some(Duration::from_millis(200))),
    );
    let mut collector = Collector::start(vec![Err(status)], None).await?;
    let mut config = collector.config();
    config.gzip = true;
    config
        .headers
        .insert("x-test", "value with spaces".parse()?);
    let exporter = LogExporter::new(config, &Default::default())?;
    exporter.submit(&event())?;
    let stats = exporter.flush().await?;
    assert_eq!((stats.exported, stats.failed, stats.retries), (1, 0, 1));
    let first = collector.receive().await?;
    let second = collector.receive().await?;
    // A 200 ms RetryInfo is followed by 50–100 ms of jitter on the first retry.
    assert!(second.at.duration_since(first.at) >= Duration::from_millis(250));
    assert_eq!(first.message, second.message);
    assert_eq!(first.metadata.get("x-test").unwrap(), "value with spaces");
    exporter.close().await?;
    collector.stop().await
}

#[tokio::test]
async fn slow_collector_cannot_exceed_item_or_byte_budgets() -> anyhow::Result<()> {
    for byte_limit in [false, true] {
        let blocked = Arc::new(Semaphore::new(0));
        let mut collector = Collector::start(vec![], Some(blocked.clone())).await?;
        let record_bytes = event().to_otlp()?.encoded_len();
        let mut config = collector.config();
        config.max_batch_records = 1;
        config.queue_capacity = if byte_limit { 10 } else { 2 };
        config.queue_bytes = if byte_limit {
            record_bytes * 2 - 1
        } else {
            1024 * 1024
        };
        let exporter = LogExporter::new(config, &Default::default())?;
        assert_eq!(exporter.submit(&event())?, Submission::Accepted);
        collector.receive().await?;
        if byte_limit {
            assert_eq!(exporter.submit(&event())?, Submission::ByteLimit);
        } else {
            assert_eq!(exporter.submit(&event())?, Submission::Accepted);
            assert_eq!(exporter.submit(&event())?, Submission::QueueFull);
        }
        let stats = exporter.stats();
        assert_eq!(stats.pending_records, if byte_limit { 1 } else { 2 });
        assert_eq!(stats.pending_bytes, stats.pending_records * record_bytes);
        assert_eq!(
            (stats.queue_full, stats.queue_bytes_full),
            if byte_limit { (0, 1) } else { (1, 0) }
        );
        blocked.add_permits(1);
        let stats = exporter.close().await?;
        assert_eq!(stats.exported, stats.accepted);
        assert_eq!((stats.pending_records, stats.pending_bytes), (0, 0));
        collector.stop().await?;
    }
    Ok(())
}

#[tokio::test]
async fn cancelled_close_keeps_its_deadline_and_accounts_for_every_record() -> anyhow::Result<()> {
    let blocked = Arc::new(Semaphore::new(0));
    let mut collector = Collector::start(vec![], Some(blocked)).await?;
    let mut config = collector.config();
    config.max_batch_records = 1;
    config.drain_timeout = Duration::from_millis(300);
    let exporter = Arc::new(LogExporter::new(config, &Default::default())?);
    exporter.submit(&event())?;
    collector.receive().await?;
    exporter.submit(&event())?;
    exporter.submit(&event())?;
    // The collector has acknowledged the real I/O; only the drain timer remains.
    tokio::time::pause();
    let mut closing = Box::pin(exporter.close());
    assert!(futures::poll!(&mut closing).is_pending());
    assert!(exporter.stats().closed);
    tokio::time::advance(Duration::from_millis(200)).await;
    drop(closing);
    // Resetting the original 300 ms deadline would exceed this remaining budget.
    let stats = tokio::time::timeout(Duration::from_millis(200), exporter.close()).await??;
    tokio::time::resume();
    assert_eq!(
        (stats.accepted, stats.timed_out, stats.shutdown_dropped),
        (3, 1, 2)
    );
    assert_eq!((stats.pending_records, stats.pending_bytes), (0, 0));
    assert_eq!(exporter.submit(&event())?, Submission::Closed);
    assert_eq!(exporter.stats().closed_dropped, 1);
    collector.stop().await
}

#[tokio::test]
async fn batch_bytes_and_timer_are_enforced() -> anyhow::Result<()> {
    let mut collector = Collector::start(vec![], None).await?;
    let mut config = collector.config();
    let record_bytes = event().to_otlp()?.encoded_len();
    config.max_batch_bytes =
        export_request(&Default::default(), &[])?.encoded_len() + 16 + (record_bytes + 8) * 2;
    config.schedule_delay = Duration::from_millis(20);
    let limit = config.max_batch_bytes;
    let exporter = LogExporter::new(config, &Default::default())?;
    // The first event reaches the collector on the timer without flush/close.
    exporter.submit(&event())?;
    assert_eq!(
        collector.receive().await?.message.resource_logs[0].scope_logs[0]
            .log_records
            .len(),
        1
    );
    for _ in 0..5 {
        exporter.submit(&event())?;
    }
    let stats = exporter.close().await?;
    assert_eq!(stats.exported, 6);
    for expected_count in [2, 2, 1] {
        let batch = collector.receive().await?.message;
        assert_eq!(
            batch.resource_logs[0].scope_logs[0].log_records.len(),
            expected_count
        );
        assert!(batch.encoded_len() <= limit);
    }
    assert!(collector.received.try_recv().is_err());
    collector.stop().await
}

#[tokio::test]
async fn outage_exhausts_only_its_batch_budget_then_recovers() -> anyhow::Result<()> {
    let mut collector = Collector::start(vec![Err(Status::unavailable("offline"))], None).await?;
    let mut config = collector.config();
    config.export_timeout = Duration::from_millis(50);
    let exporter = LogExporter::new(config, &Default::default())?;
    exporter.submit(&event())?;
    let stats = exporter.flush().await?;
    assert_eq!((stats.timed_out, stats.exported, stats.retries), (1, 0, 0));
    collector.receive().await?;
    exporter.submit(&event())?;
    let stats = exporter.close().await?;
    assert_eq!((stats.accepted, stats.timed_out, stats.exported), (2, 1, 1));
    collector.receive().await?;
    assert!(collector.received.try_recv().is_err());
    collector.stop().await
}
