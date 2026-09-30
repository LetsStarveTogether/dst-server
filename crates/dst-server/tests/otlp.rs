use std::{
    collections::VecDeque,
    sync::{Arc, Mutex},
    time::Duration,
};

use dst_server::telemetry::{
    ExportConfig, LogEvent, LogExporter, Submission, any_value, export_request,
};
use opentelemetry_proto::tonic::{
    collector::logs::v1::{
        ExportLogsPartialSuccess, ExportLogsServiceRequest, ExportLogsServiceResponse,
        logs_service_client::LogsServiceClient,
        logs_service_server::{LogsService, LogsServiceServer},
    },
    common::v1::{AnyValue, KeyValue, any_value::Value as Proto},
    logs::v1::SeverityNumber,
};
use prost::Message;
use serde_json::{Value, json};
use tokio::{
    sync::{Semaphore, mpsc, oneshot},
    task::JoinHandle,
    time::Instant,
};
use tokio_stream::wrappers::TcpListenerStream;
use tonic::{
    Code, Request, Response, Status, codec::CompressionEncoding, metadata::MetadataMap,
    transport::Server,
};
use tonic_types::{ErrorDetails, StatusExt};

struct Capture(mpsc::Sender<ExportLogsServiceRequest>);

#[tonic::async_trait]
impl LogsService for Capture {
    async fn export(
        &self,
        request: Request<ExportLogsServiceRequest>,
    ) -> Result<Response<ExportLogsServiceResponse>, Status> {
        self.0
            .try_send(request.into_inner())
            .map_err(|error| Status::resource_exhausted(error.to_string()))?;
        Ok(Response::new(ExportLogsServiceResponse::default()))
    }
}

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
            event_name: "dst.player.action".into(),
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
                "userid": "18446744073709551616"
            }),
            observed_timestamp_ns: 1_791_013_234_567_890_123,
            severity_text: "info".into(),
            attributes: json!({"log.record.uid": "attempt:generation:42", "optional": null})
                .as_object().unwrap().clone(),
        };
        let resource = json!({"service.name": "dst-server", "dst.cluster.name": "房间"});
        let expected = export_request(resource.as_object().unwrap(), std::slice::from_ref(&event))?;
        let decoded = ExportLogsServiceRequest::decode(expected.encode_to_vec().as_slice())?;
        assert_eq!(decoded, expected);

        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let endpoint = format!("http://{}", listener.local_addr()?);
        let (received_tx, mut received_rx) = mpsc::channel(1);
        let (shutdown_tx, shutdown_rx) = oneshot::channel::<()>();
        let server = tokio::spawn(
            Server::builder()
                .add_service(LogsServiceServer::new(Capture(received_tx)))
                .serve_with_incoming_shutdown(TcpListenerStream::new(listener), async {
                    let _ = shutdown_rx.await;
                }),
        );
        let mut client = LogsServiceClient::connect(endpoint).await?;
        let response = client.export(expected.clone()).await?.into_inner();
        assert!(response.partial_success.is_none());
        let received = received_rx.recv().await.unwrap();
        assert_eq!(received, expected);
        let _ = shutdown_tx.send(());
        server.await??;

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
        event_name: String::new(),
        severity_text: "INFO".into(),
        ..invalid
    };
    assert!(empty_name.to_otlp().is_err());
    Ok(())
}

struct CapturedRequest {
    message: ExportLogsServiceRequest,
    metadata: MetadataMap,
    at: Instant,
}

struct ControlledCapture {
    received: mpsc::Sender<CapturedRequest>,
    replies: Mutex<VecDeque<Result<ExportLogsServiceResponse, Status>>>,
    blocked: Option<Arc<Semaphore>>,
}

#[tonic::async_trait]
impl LogsService for ControlledCapture {
    async fn export(
        &self,
        request: Request<ExportLogsServiceRequest>,
    ) -> Result<Response<ExportLogsServiceResponse>, Status> {
        let metadata = request.metadata().clone();
        self.received
            .try_send(CapturedRequest {
                message: request.into_inner(),
                metadata,
                at: Instant::now(),
            })
            .map_err(|error| Status::resource_exhausted(error.to_string()))?;
        if let Some(blocked) = &self.blocked {
            let _permit = blocked.acquire().await.unwrap();
        }
        self.replies
            .lock()
            .unwrap()
            .pop_front()
            .unwrap_or_else(|| Ok(ExportLogsServiceResponse::default()))
            .map(Response::new)
    }
}

struct Collector {
    endpoint: String,
    received: mpsc::Receiver<CapturedRequest>,
    shutdown: oneshot::Sender<()>,
    task: JoinHandle<Result<(), tonic::transport::Error>>,
    blocked: Option<Arc<Semaphore>>,
}

impl Collector {
    async fn start(
        replies: Vec<Result<ExportLogsServiceResponse, Status>>,
        blocked: Option<Arc<Semaphore>>,
    ) -> anyhow::Result<Self> {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let endpoint = format!("http://{}", listener.local_addr()?);
        let (received_tx, received) = mpsc::channel(64);
        let (shutdown, shutdown_rx) = oneshot::channel();
        let service = ControlledCapture {
            received: received_tx,
            replies: Mutex::new(replies.into()),
            blocked: blocked.clone(),
        };
        let task = tokio::spawn(
            Server::builder()
                .add_service(
                    LogsServiceServer::new(service).accept_compressed(CompressionEncoding::Gzip),
                )
                .serve_with_incoming_shutdown(TcpListenerStream::new(listener), async {
                    let _ = shutdown_rx.await;
                }),
        );
        Ok(Self {
            endpoint,
            received,
            shutdown,
            task,
            blocked,
        })
    }

    fn config(&self) -> ExportConfig {
        ExportConfig {
            endpoint: self.endpoint.clone(),
            schedule_delay: Duration::from_secs(60),
            export_timeout: Duration::from_secs(2),
            drain_timeout: Duration::from_secs(3),
            ..Default::default()
        }
    }

    async fn receive(&mut self) -> anyhow::Result<CapturedRequest> {
        Ok(
            tokio::time::timeout(Duration::from_secs(2), self.received.recv())
                .await?
                .unwrap(),
        )
    }

    async fn stop(self) -> anyhow::Result<()> {
        if let Some(blocked) = self.blocked {
            blocked.add_permits(1);
        }
        let _ = self.shutdown.send(());
        tokio::time::timeout(Duration::from_secs(2), self.task).await???;
        Ok(())
    }
}

fn event() -> LogEvent {
    LogEvent {
        event_name: "dst.player.action".into(),
        body: json!({"userid": "KU_test", "null": null}),
        observed_timestamp_ns: 1_791_013_234_567_890_123,
        severity_text: "INFO".into(),
        attributes: Default::default(),
    }
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
    assert!(second.at.duration_since(first.at) >= Duration::from_millis(200));
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
    let closing = tokio::spawn({
        let exporter = exporter.clone();
        async move { exporter.close().await }
    });
    tokio::task::yield_now().await;
    assert!(exporter.stats().closed);
    tokio::time::sleep(Duration::from_millis(200)).await;
    closing.abort();
    // The original deadline has about 100 ms left. Resetting it here to a new
    // 300 ms budget must fail this wait, even though the first waiter cancelled.
    let stats = tokio::time::timeout(Duration::from_millis(200), exporter.close()).await??;
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
