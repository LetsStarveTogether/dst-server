use std::{
    collections::{BTreeMap, VecDeque},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};

use dst_server::{
    driver::{DriverEvent, DriverEventKind, DriverState},
    model::{Error, ErrorCode},
    observability::Observability,
    telemetry::{ExportConfig, TelemetryConfig},
};
use opentelemetry_proto::tonic::{
    collector::{
        metrics::v1::{
            ExportMetricsServiceRequest, ExportMetricsServiceResponse,
            metrics_service_server::{MetricsService, MetricsServiceServer},
        },
        trace::v1::{
            ExportTraceServiceRequest, ExportTraceServiceResponse,
            trace_service_server::{TraceService, TraceServiceServer},
        },
    },
    common::v1::{KeyValue, any_value::Value as AnyValue},
    metrics::v1::{Metric, metric::Data, number_data_point::Value as Number},
    trace::v1::Span,
};
use serde_json::{Value, json};
use tokio::{net::TcpListener, sync::Semaphore, task::JoinHandle};
use tokio_stream::wrappers::TcpListenerStream;
use tonic::{Request, Response, Status, codec::CompressionEncoding, transport::Server};

#[derive(Clone)]
struct Capture {
    metrics: Arc<Mutex<Vec<ExportMetricsServiceRequest>>>,
    traces: Arc<Mutex<Vec<ExportTraceServiceRequest>>>,
    pause: Arc<AtomicBool>,
    entered: Arc<Semaphore>,
    release: Arc<Semaphore>,
}

#[tonic::async_trait]
impl MetricsService for Capture {
    async fn export(
        &self,
        request: Request<ExportMetricsServiceRequest>,
    ) -> Result<Response<ExportMetricsServiceResponse>, Status> {
        assert_eq!(
            request.metadata().get("authorization").unwrap(),
            "fixture-key"
        );
        self.metrics.lock().unwrap().push(request.into_inner());
        if self.pause.load(Ordering::SeqCst) {
            self.entered.add_permits(1);
            self.release.acquire().await.unwrap().forget();
        }
        Ok(Response::new(ExportMetricsServiceResponse {
            partial_success: None,
        }))
    }
}

#[tonic::async_trait]
impl TraceService for Capture {
    async fn export(
        &self,
        request: Request<ExportTraceServiceRequest>,
    ) -> Result<Response<ExportTraceServiceResponse>, Status> {
        assert_eq!(
            request.metadata().get("authorization").unwrap(),
            "fixture-key"
        );
        self.traces.lock().unwrap().push(request.into_inner());
        Ok(Response::new(ExportTraceServiceResponse {
            partial_success: None,
        }))
    }
}

struct Collector {
    endpoint: String,
    capture: Capture,
    task: JoinHandle<Result<(), tonic::transport::Error>>,
}
impl Drop for Collector {
    fn drop(&mut self) {
        self.task.abort();
    }
}
impl Collector {
    async fn start() -> anyhow::Result<Self> {
        let listener = TcpListener::bind("127.0.0.1:0").await?;
        let endpoint = format!("http://{}", listener.local_addr()?);
        let capture = Capture {
            metrics: Arc::new(Mutex::new(Vec::new())),
            traces: Arc::new(Mutex::new(Vec::new())),
            pause: Arc::new(AtomicBool::new(false)),
            entered: Arc::new(Semaphore::new(0)),
            release: Arc::new(Semaphore::new(0)),
        };
        let task = tokio::spawn(
            Server::builder()
                .add_service(
                    MetricsServiceServer::new(capture.clone())
                        .accept_compressed(CompressionEncoding::Gzip),
                )
                .add_service(
                    TraceServiceServer::new(capture.clone())
                        .accept_compressed(CompressionEncoding::Gzip),
                )
                .serve_with_incoming(TcpListenerStream::new(listener)),
        );
        Ok(Self {
            endpoint,
            capture,
            task,
        })
    }

    fn config(&self) -> anyhow::Result<TelemetryConfig> {
        let variables = BTreeMap::from([
            ("OTEL_LOGS_EXPORTER", "none".to_owned()),
            ("OTEL_EXPORTER_OTLP_ENDPOINT", self.endpoint.clone()),
            (
                "OTEL_EXPORTER_OTLP_HEADERS",
                "authorization=fixture-key".to_owned(),
            ),
            ("OTEL_EXPORTER_OTLP_TIMEOUT", "2000".to_owned()),
            ("OTEL_METRIC_EXPORT_INTERVAL", "3600000".to_owned()),
            ("OTEL_BSP_SCHEDULE_DELAY", "3600000".to_owned()),
        ]);
        TelemetryConfig::from_lookup(|name| variables.get(name).cloned())
    }

    fn latest_metrics(&self) -> BTreeMap<String, Metric> {
        self.capture
            .metrics
            .lock()
            .unwrap()
            .last()
            .unwrap()
            .resource_metrics
            .iter()
            .flat_map(|resource| &resource.scope_metrics)
            .flat_map(|scope| &scope.metrics)
            .map(|metric| (metric.name.clone(), metric.clone()))
            .collect()
    }

    fn spans(&self) -> Vec<Span> {
        self.capture
            .traces
            .lock()
            .unwrap()
            .iter()
            .flat_map(|request| &request.resource_spans)
            .flat_map(|resource| &resource.scope_spans)
            .flat_map(|scope| scope.spans.iter().cloned())
            .collect()
    }
}

fn attribute<'a>(values: &'a [KeyValue], name: &str) -> Option<&'a AnyValue> {
    values
        .iter()
        .find(|value| value.key == name)
        .and_then(|value| value.value.as_ref())
        .and_then(|value| value.value.as_ref())
}

fn sum(metric: &Metric) -> i64 {
    let Some(Data::Sum(sum)) = &metric.data else {
        panic!("metric is not a sum")
    };
    sum.data_points
        .iter()
        .map(|point| match point.value {
            Some(Number::AsInt(value)) => value,
            _ => panic!("expected exact integer metric"),
        })
        .sum()
}

fn source() -> DriverState {
    DriverState {
        shard: "Master".into(),
        pid: 1,
        nonce: "attempt-one".into(),
        generation: Some(1),
        generation_changes: 0,
        native_ready: true,
        ready: true,
        running: true,
        stopping: false,
        returncode: None,
        forced: false,
        output_drained: false,
        load_failure: None,
        session_id: Some("session-one".into()),
        health: None,
        runtime: None,
        last_native_save: None,
        last_control: None,
        control_records: VecDeque::new(),
        failure: None,
        telemetry_sequence: 0,
        telemetry_gaps: 5,
        invalid_telemetry: 2,
        stale_telemetry: 1,
        unmatched_replies: 0,
        recent_request: None,
    }
}

fn event(source: &DriverState, name: &str, data: Value) -> DriverEvent {
    DriverEvent {
        kind: DriverEventKind::Telemetry,
        shard: source.shard.clone(),
        nonce: source.nonce.clone(),
        observed_timestamp_ns: 1234,
        generation: source.generation,
        data: json!({"event":name,"data":data}),
    }
}

#[tokio::test(flavor = "current_thread")]
async fn official_sdk_exports_six_instruments_and_operation_spans() -> anyhow::Result<()> {
    let collector = Collector::start().await?;
    let config = collector.config()?;
    let observability = Observability::new(&config)?;
    let mut source = source();
    observability.observe_state(&source, "cluster");
    observability.observe_state(&source, "cluster");
    observability.observe_driver(&event(&source, "dst.server.presence", json!({
        "players":[{"guid":1,"userid":"KU_A"},{"guid":2,"userid":"KU_A"},{"guid":3,"userid":"KU_B"}],
        "clients":["KU_A","KU_A","KU_B"]
    })), "cluster");
    observability.observe_driver(
        &event(
            &source,
            "dst.player.action",
            json!({"action_id":"ATTACK","success":false}),
        ),
        "cluster",
    );
    observability.record_subscription_drop("cluster", "Master", 3);
    observability
        .begin_operation("cluster", Some("Master"), "save", Some("session-one"))
        .finish(None);
    observability
        .begin_operation("cluster", Some("Master"), "rollback", None)
        .finish(Some(&Error::new(
            ErrorCode::Timeout,
            "private error details must remain outside attributes",
        )));
    drop(observability.begin_operation("cluster", None, "cancelled", None));
    observability.flush().await?;
    let metrics = collector.latest_metrics();
    assert_eq!(metrics.len(), 6);
    assert_eq!(sum(&metrics["dst.server.process.count"]), 1);
    assert_eq!(sum(&metrics["dst.server.player.count"]), 2);
    assert_eq!(sum(&metrics["dst.server.client.count"]), 2);
    assert_eq!(sum(&metrics["dst.player.action.count"]), 1);
    assert_eq!(sum(&metrics["dst.telemetry.event.count"]), 13);
    let Some(Data::Sum(actions)) = &metrics["dst.player.action.count"].data else {
        unreachable!()
    };
    assert_eq!(
        attribute(&actions.data_points[0].attributes, "dst.action.success"),
        Some(&AnyValue::BoolValue(false))
    );
    let Some(Data::Histogram(duration)) = &metrics["dst.server.operation.duration"].data else {
        panic!("missing histogram")
    };
    assert_eq!(
        duration
            .data_points
            .iter()
            .map(|point| point.count)
            .sum::<u64>(),
        3
    );
    assert!(
        duration
            .data_points
            .iter()
            .all(|point| point.sum.is_some_and(|sum| sum >= 0.0))
    );
    let spans = collector.spans();
    assert_eq!(spans.len(), 3);
    let rollback = spans
        .iter()
        .find(|span| span.name == "dst.server.rollback")
        .unwrap();
    assert_eq!(
        attribute(&rollback.attributes, "error.type"),
        Some(&AnyValue::StringValue("timeout".into()))
    );
    assert!(!format!("{spans:?}").contains("private error details"));
    assert_eq!(
        attribute(
            &spans
                .iter()
                .find(|span| span.name == "dst.server.save")
                .unwrap()
                .attributes,
            "dst.session.id"
        ),
        Some(&AnyValue::StringValue("session-one".into()))
    );
    let cancelled = spans
        .iter()
        .find(|span| span.name == "dst.server.cancelled")
        .unwrap();
    assert_eq!(
        attribute(&cancelled.attributes, "error.type"),
        Some(&AnyValue::StringValue("cancelled".into()))
    );
    for request in collector.capture.metrics.lock().unwrap().iter() {
        assert_eq!(
            attribute(
                &request.resource_metrics[0]
                    .resource
                    .as_ref()
                    .unwrap()
                    .attributes,
                "service.instance.id"
            ),
            Some(&AnyValue::StringValue(
                config.resource["service.instance.id"]
                    .as_str()
                    .unwrap()
                    .into()
            ))
        );
    }
    for request in collector.capture.traces.lock().unwrap().iter() {
        assert_eq!(
            attribute(
                &request.resource_spans[0]
                    .resource
                    .as_ref()
                    .unwrap()
                    .attributes,
                "service.instance.id"
            ),
            Some(&AnyValue::StringValue(
                config.resource["service.instance.id"]
                    .as_str()
                    .unwrap()
                    .into()
            ))
        );
    }
    source.generation = Some(2);
    observability.observe_state(&source, "cluster");
    source.running = false;
    observability.observe_state(&source, "cluster");
    observability.flush().await?;
    let metrics = collector.latest_metrics();
    assert_eq!(sum(&metrics["dst.server.process.count"]), 0);
    assert_eq!(sum(&metrics["dst.server.player.count"]), 0);
    assert_eq!(sum(&metrics["dst.server.client.count"]), 0);
    assert_eq!(sum(&metrics["dst.telemetry.event.count"]), 13);
    observability.close().await?;
    observability.close().await?;
    Ok(())
}

#[tokio::test(flavor = "current_thread")]
async fn cancelled_close_keeps_sdk_shutdown_running() -> anyhow::Result<()> {
    let collector = Collector::start().await?;
    let observability = Arc::new(Observability::new(&collector.config()?)?);
    observability.observe_state(&source(), "cluster");
    collector.capture.pause.store(true, Ordering::SeqCst);
    let closing = {
        let observability = observability.clone();
        tokio::spawn(async move { observability.close().await })
    };
    tokio::time::timeout(Duration::from_secs(3), collector.capture.entered.acquire())
        .await??
        .forget();
    closing.abort();
    collector.capture.release.add_permits(1);
    tokio::time::timeout(Duration::from_secs(3), observability.close()).await??;
    Ok(())
}

#[tokio::test]
async fn disabled_signals_create_no_exporters() -> anyhow::Result<()> {
    let config =
        TelemetryConfig::from_lookup(|name| (name == "OTEL_SDK_DISABLED").then(|| "true".into()))?;
    assert!(config.logs.is_none() && config.metrics.is_none() && config.traces.is_none());
    let observability = Observability::new(&config)?;
    observability.observe_state(&source(), "cluster");
    observability
        .begin_operation("cluster", None, "save", None)
        .finish(None);
    observability.flush().await?;
    observability.close().await?;
    Ok(())
}

#[test]
fn unsupported_limits_are_rejected_before_any_worker_is_started() -> anyhow::Result<()> {
    let mut config =
        TelemetryConfig::from_lookup(|name| (name == "OTEL_SDK_DISABLED").then(|| "true".into()))?;
    config.traces = Some(ExportConfig {
        queue_capacity: usize::MAX,
        ..ExportConfig::default()
    });
    assert!(
        Observability::new(&config)
            .err()
            .unwrap()
            .to_string()
            .contains("queue capacity")
    );
    config.traces = None;
    config
        .resource
        .insert("invalid".into(), serde_json::from_str("1e999")?);
    assert!(Observability::new(&config).is_err());
    Ok(())
}
