use std::{
    collections::{BTreeMap, VecDeque},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, Ordering},
    },
    time::Duration,
};

use dst_server::{
    driver::DriverState,
    events::RejectionReason,
    model::{Error, ErrorCode, SessionIdentity},
    observability::Observability,
    policy::Presence,
    telemetry::{ExportConfig, TelemetryConfig},
};
use opentelemetry_proto::tonic::{
    collector::{
        metrics::v1::{
            ExportMetricsPartialSuccess, ExportMetricsServiceRequest, ExportMetricsServiceResponse,
            metrics_service_server::{MetricsService, MetricsServiceServer},
        },
        trace::v1::{
            ExportTracePartialSuccess, ExportTraceServiceRequest, ExportTraceServiceResponse,
            trace_service_server::{TraceService, TraceServiceServer},
        },
    },
    common::v1::{KeyValue, any_value::Value as AnyValue},
    metrics::v1::{Metric, metric::Data, number_data_point::Value as Number},
    trace::v1::Span,
};
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
    metric_replies: Arc<Mutex<VecDeque<Result<ExportMetricsServiceResponse, Status>>>>,
    trace_replies: Arc<Mutex<VecDeque<Result<ExportTraceServiceResponse, Status>>>>,
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
        self.metric_replies
            .lock()
            .unwrap()
            .pop_front()
            .unwrap_or_else(|| Ok(ExportMetricsServiceResponse::default()))
            .map(Response::new)
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
        self.trace_replies
            .lock()
            .unwrap()
            .pop_front()
            .unwrap_or_else(|| Ok(ExportTraceServiceResponse::default()))
            .map(Response::new)
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
            metric_replies: Default::default(),
            trace_replies: Default::default(),
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

fn gauge(metric: &Metric) -> Option<Number> {
    let Some(Data::Gauge(gauge)) = &metric.data else {
        panic!("expected a gauge")
    };
    assert_eq!(gauge.data_points.len(), 1);
    gauge.data_points[0].value
}

fn source() -> DriverState {
    DriverState {
        shard: "Master".into(),
        pid: 1,
        process_id: ulid::Ulid::generate().to_string(),
        nonce: "attempt-one".into(),
        generation: Some(1),
        generation_changes: 0,
        native_ready: true,
        ready: true,
        running: true,
        stopping: false,
        exit: None,
        native_diagnostics: Default::default(),
        load_failure: None,
        session_id: Some("session-one".into()),
        health: None,
        runtime: None,
        last_native_save: None,
        last_control: None,
        control_count: 0,
        failure: None,
        telemetry_sequence: 6,
        telemetry_gaps: 5,
        invalid_telemetry: 1,
        stale_telemetry: 1,
        telemetry_rejections: BTreeMap::from([
            (RejectionReason::JsonSyntax, 1),
            (RejectionReason::Sequence, 1),
        ]),
        last_telemetry_rejection: None,
        unmatched_replies: 0,
        recent_request: None,
    }
}

#[tokio::test(flavor = "current_thread")]
async fn exports_polled_presence_event_counts_and_operation_spans() -> anyhow::Result<()> {
    let collector = Collector::start().await?;
    let config = collector.config()?;
    let observability = Observability::new(&config)?;
    let mut source = source();
    observability.observe_state(&source, "cluster");
    observability.observe_state(&source, "cluster");
    let identity = SessionIdentity {
        process_id: source.process_id.clone(),
        generation: source.generation.unwrap(),
        session_id: source.session_id.clone().unwrap(),
    };
    let mut reading = Presence {
        session_id: identity.session_id.clone(),
        observation: "poll".into(),
        client_count: 3,
        player_count: 2,
        idle_seconds: 0.0,
        observed_seconds: 30.0,
        reliable: true,
    };
    let poll_started = chrono::Utc::now().timestamp();
    observability.observe_presence("cluster", "Master", &identity, Some(&reading));
    for _ in 0..2 {
        observability.observe_gameplay("cluster", &source.shard, "dst.client.authenticated");
    }
    observability.record_relay_drop("cluster", "Master", 3);
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
    observability
        .begin_operation("cluster", None, "interrupted", None)
        .finish(Some(&Error::new(ErrorCode::Unknown, "interrupted")));
    let panicked = observability.begin_operation("cluster", None, "panicked", None);
    assert!(
        tokio::spawn(async move {
            let _observation = panicked;
            panic!("fixture panic")
        })
        .await
        .unwrap_err()
        .is_panic()
    );
    observability.flush().await?;
    let metrics = collector.latest_metrics();
    let clients = "dst.server.client.connection.count";
    let entities = "dst.server.player.entity.count";
    let freshness = "dst.server.presence.last_observed";
    assert_eq!(sum(&metrics["dst.server.process.count"]), 1);
    assert_eq!(gauge(&metrics[clients]), Some(Number::AsInt(3)));
    assert_eq!(gauge(&metrics[entities]), Some(Number::AsInt(2)));
    let Some(Number::AsInt(observed)) = gauge(&metrics[freshness]) else {
        panic!("missing poll timestamp");
    };
    assert!((poll_started..=chrono::Utc::now().timestamp()).contains(&observed));
    assert_eq!(sum(&metrics["dst.telemetry.event.count"]), 4);
    assert_eq!(sum(&metrics["dst.telemetry.sequence.gap.count"]), 5);
    let Some(Data::Sum(delivery)) = &metrics["dst.telemetry.delivery.count"].data else {
        panic!("missing delivery count");
    };
    let [relay] = delivery.data_points.as_slice() else {
        panic!("expected one relay");
    };
    assert_eq!(relay.value, Some(Number::AsInt(3)));
    for (key, value) in [
        ("dst.cluster.name", "cluster"),
        ("dst.shard.name", "Master"),
        ("dst.telemetry.stage", "driver_relay"),
        ("dst.telemetry.source", "driver"),
        ("dst.telemetry.reason", "dropped"),
    ] {
        assert_eq!(
            attribute(&relay.attributes, key),
            Some(&AnyValue::StringValue(value.into()))
        );
    }
    let metric = &metrics["dst.server.operation.duration"];
    assert_eq!(metric.unit, "s");
    let Some(Data::Histogram(duration)) = &metric.data else {
        panic!("missing histogram")
    };
    assert_eq!(duration.data_points.len(), 5);
    for point in &duration.data_points {
        assert_eq!(point.count, 1);
        let seconds = point.sum.unwrap();
        assert!(seconds >= 0.0);
        let upper = point
            .explicit_bounds
            .partition_point(|&bound| bound < seconds);
        assert_eq!(point.bucket_counts[upper], 1);
        for bound in [0.005, 0.025, 0.25, 2.5, 30.0, 300.0] {
            assert!(
                point.explicit_bounds.contains(&bound),
                "missing {bound}s boundary"
            );
        }
    }
    let spans = collector.spans();
    assert_eq!(spans.len(), 5);
    for (name, outcome, error_type) in [
        ("dst.server.save", "completed", None),
        ("dst.server.rollback", "failure", Some("timeout")),
        ("dst.server.interrupted", "unknown", Some("unknown")),
        ("dst.server.cancelled", "cancelled", Some("cancelled")),
        ("dst.server.panicked", "panic", Some("panic")),
    ] {
        let span = spans.iter().find(|span| span.name == name).unwrap();
        assert_eq!(
            attribute(&span.attributes, "dst.operation.outcome"),
            Some(&AnyValue::StringValue(outcome.into()))
        );
        assert_eq!(
            attribute(&span.attributes, "error.type").cloned(),
            error_type.map(|error| AnyValue::StringValue(error.into()))
        );
    }
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
    let instance = attribute(
        &collector.capture.metrics.lock().unwrap()[0].resource_metrics[0]
            .resource
            .as_ref()
            .unwrap()
            .attributes,
        "service.instance.id",
    )
    .cloned()
    .unwrap();
    assert!(matches!(&instance, AnyValue::StringValue(id) if id.parse::<ulid::Ulid>().is_ok()));
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
            Some(&instance)
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
            Some(&instance)
        );
    }
    observability.observe_presence("cluster", "Master", &identity, None);
    observability.flush().await?;
    let metrics = collector.latest_metrics();
    assert!(
        [clients, entities, freshness]
            .iter()
            .all(|&key| !metrics.contains_key(key))
    );

    reading.client_count = 0;
    reading.player_count = 0;
    observability.observe_presence("cluster", "Master", &identity, Some(&reading));
    observability.flush().await?;
    let metrics = collector.latest_metrics();
    assert_eq!(gauge(&metrics[clients]), Some(Number::AsInt(0)));
    assert_eq!(gauge(&metrics[entities]), Some(Number::AsInt(0)));

    source.generation = Some(2);
    source.invalid_telemetry += 4;
    source
        .telemetry_rejections
        .insert(RejectionReason::Schema, 4);
    observability.observe_state(&source, "cluster");
    observability.observe_state(&source, "cluster");
    source.running = false;
    observability.observe_state(&source, "cluster");
    observability.observe_presence("cluster", "Master", &identity, Some(&reading));
    observability.flush().await?;
    let metrics = collector.latest_metrics();
    assert_eq!(sum(&metrics["dst.server.process.count"]), 0);
    assert!(
        [clients, entities, freshness]
            .iter()
            .all(|&key| !metrics.contains_key(key))
    );
    assert_eq!(sum(&metrics["dst.telemetry.event.count"]), 8);
    // A reload keeps cumulative process counters; a new process starts a new delta.
    source.process_id = ulid::Ulid::generate().to_string();
    source.nonce = "attempt-two".into();
    source.telemetry_sequence = 0;
    source.telemetry_gaps = 0;
    source.invalid_telemetry = 1;
    source.stale_telemetry = 0;
    source.telemetry_rejections = BTreeMap::from([(RejectionReason::Schema, 1)]);
    observability.observe_state(&source, "cluster");
    observability.observe_state(&source, "cluster");
    observability.flush().await?;
    let metrics = collector.latest_metrics();
    assert_eq!(sum(&metrics["dst.telemetry.sequence.gap.count"]), 5);
    assert_eq!(sum(&metrics["dst.telemetry.delivery.count"]), 3);
    let Some(Data::Sum(events)) = &metrics["dst.telemetry.event.count"].data else {
        unreachable!()
    };
    let counts: BTreeMap<_, _> = events
        .data_points
        .iter()
        .map(|point| {
            let label = |key| match attribute(&point.attributes, key) {
                Some(AnyValue::StringValue(value)) => value.as_str(),
                _ => panic!("missing {key}"),
            };
            let outcome = label("dst.telemetry.outcome");
            let detail = match outcome {
                "accepted" => label("dst.event.name"),
                "rejected" => label("dst.telemetry.reason"),
                _ => panic!("unexpected outcome {outcome}"),
            };
            let Some(Number::AsInt(count)) = point.value else {
                panic!("expected integer count")
            };
            assert_eq!(point.attributes.len(), 4);
            ((outcome, detail), count)
        })
        .collect();
    assert_eq!(
        counts,
        BTreeMap::from([
            (("accepted", "dst.client.authenticated"), 2),
            (("rejected", "json_syntax"), 1),
            (("rejected", "sequence"), 1),
            (("rejected", "schema"), 5),
        ])
    );
    observability.close().await?;
    observability.close().await?;
    Ok(())
}

#[path = "fixtures/log_collector.rs"]
mod log_collector;
#[path = "fixtures/stopped_room.rs"]
mod stopped_room;

async fn final_log_metrics(cancel_wait: bool) -> anyhow::Result<()> {
    use dst_server::model::{Envelope, Request, Target};

    let collector = Collector::start().await?;
    let mut logs =
        log_collector::Collector::start(vec![], Some(Arc::new(Semaphore::new(0)))).await?;
    let mut config = collector.config()?;
    config.logs = Some(ExportConfig {
        endpoint: logs.endpoint.clone(),
        max_batch_records: 1,
        export_timeout: Duration::from_secs(10),
        drain_timeout: Duration::from_secs(2),
        ..Default::default()
    });
    let (_directory, room) = stopped_room::open(config)?;
    let error = room
        .invoke(Envelope::new(Target::Room, Request::Save {})?)
        .await
        .unwrap_err();
    assert_eq!(error.code, ErrorCode::NotReady);
    logs.receive().await?;
    let snapshot = |metrics: &BTreeMap<String, Metric>| {
        let number = |name: &str, stage: &str, reason: &str| {
            let points = match metrics[name].data.as_ref().unwrap() {
                Data::Sum(sum) => &sum.data_points,
                Data::Gauge(gauge) => &gauge.data_points,
                _ => panic!("unexpected metric kind for {name}"),
            };
            let points: Vec<_> = points
                .iter()
                .filter(|point| {
                    [("stage", stage), ("source", "logs"), ("reason", reason)]
                        .into_iter()
                        .all(|(key, value)| {
                            attribute(&point.attributes, &format!("dst.telemetry.{key}"))
                                == Some(&AnyValue::StringValue(value.into()))
                        })
                })
                .collect();
            assert_eq!(points.len(), 1, "{name}/{stage}/{reason}");
            let Some(Number::AsInt(value)) = points[0].value else {
                panic!("expected exact integer metric")
            };
            value
        };
        [
            number(
                "dst.telemetry.delivery.count",
                "export",
                "confirmation_timeout",
            ),
            number("dst.telemetry.delivery.count", "export", "shutdown_dropped"),
            number("dst.telemetry.pending.records", "otlp", "pending"),
            number("dst.telemetry.pending.bytes", "otlp", "pending"),
        ]
    };
    let before = &room.telemetry_stats()["exporter"];
    assert_eq!(before["pending_records"], 2);
    assert!(before["pending_bytes"].as_u64().unwrap() > 0);
    let batches_before = collector.capture.metrics.lock().unwrap().len();
    let mut closing = Box::pin(room.close_telemetry());
    assert!(futures::poll!(&mut closing).is_pending());
    tokio::time::timeout(Duration::from_secs(1), async {
        while room.telemetry_stats()["exporter"]["closed"] != true {
            tokio::task::yield_now().await;
        }
    })
    .await?;
    let report = if cancel_wait {
        tokio::time::sleep(Duration::from_millis(1200)).await;
        drop(closing);
        // A fresh two-second drain would exceed this remaining wait.
        tokio::time::timeout(Duration::from_millis(1400), room.close_telemetry()).await??
    } else {
        tokio::time::timeout(Duration::from_secs(4), closing).await??
    };
    let after = snapshot(&collector.latest_metrics());
    let batches_after = collector.capture.metrics.lock().unwrap().len();
    let repeated = room.close_telemetry().await?;
    assert_eq!(repeated, report);
    assert_eq!(
        collector.capture.metrics.lock().unwrap().len(),
        batches_after
    );
    logs.stop().await?;
    assert_eq!(batches_after, batches_before + 1);
    assert_eq!(report["relays_drained"], true);
    assert_eq!(report["output_drained"], true);
    assert!(report["export_error"].is_null());
    assert!(report["observability_error"].is_null());
    let stats = &report["stats"]["exporter"];
    let settled = [
        "timed_out",
        "shutdown_dropped",
        "pending_records",
        "pending_bytes",
    ]
    .map(|field| stats[field].as_i64().unwrap());
    assert_eq!(settled, [1, 1, 0, 0]);
    assert_eq!(
        after, settled,
        "last metrics must observe settled log delivery"
    );
    Ok(())
}

#[tokio::test(flavor = "current_thread")]
async fn room_close_exports_settled_log_delivery_metrics() -> anyhow::Result<()> {
    final_log_metrics(false).await
}

#[tokio::test]
async fn room_exports_metrics_when_another_exporter_cannot_start() -> anyhow::Result<()> {
    use dst_server::model::{Envelope, Request, Target};

    let collector = Collector::start().await?;
    let mut config = collector.config()?;
    config.traces.as_mut().unwrap().queue_capacity = 0;
    let (_directory, room) = stopped_room::open(config)?;
    let failure = room
        .invoke(Envelope::new(Target::Room, Request::Save {})?)
        .await
        .unwrap_err();
    assert_eq!(failure.code, ErrorCode::NotReady);
    room.close_telemetry().await?;
    assert!(
        collector
            .latest_metrics()
            .contains_key("dst.server.operation.duration")
    );
    assert!(collector.capture.traces.lock().unwrap().is_empty());
    Ok(())
}

#[tokio::test(flavor = "current_thread")]
async fn cancelled_room_close_exports_settled_log_delivery_metrics() -> anyhow::Result<()> {
    final_log_metrics(true).await
}

#[tokio::test]
async fn sdk_partial_success_preserves_call_semantics_and_logs_link_to_real_spans()
-> anyhow::Result<()> {
    use dst_server::model::{Envelope, Request as Operation, Target};
    let collector = Collector::start().await?;
    let mut logs = log_collector::Collector::start(vec![], None).await?;
    let mut config = collector.config()?;
    config.logs = Some(ExportConfig {
        endpoint: logs.endpoint.clone(),
        schedule_delay: Duration::from_secs(60),
        ..Default::default()
    });
    let (_directory, room) = stopped_room::open(config)?;
    room.invoke(Envelope::new(
        Target::Room,
        Operation::Ban {
            userid: "KU_TARGET".into(),
            seconds: None,
        },
    )?)
    .await?;
    let vote = room
        .invoke(Envelope::new(
            Target::Room,
            Operation::StartVote {
                command: "kick".into(),
                starter_userid: "KU_STARTER".into(),
                target_userid: Some("KU_NOMINEE".into()),
            },
        )?)
        .await;
    assert_eq!(vote.unwrap_err().code, ErrorCode::NotReady);
    room.close_telemetry().await?;
    let received = logs.receive().await?;
    assert!(received.metadata.get("authorization").is_none());
    assert!(received.at.elapsed() < Duration::from_secs(10));
    let spans = collector.spans();
    assert_eq!(spans.len(), 2);
    let instance = attribute(
        &received.message.resource_logs[0]
            .resource
            .as_ref()
            .unwrap()
            .attributes,
        "service.instance.id",
    )
    .unwrap();
    assert!(matches!(instance, AnyValue::StringValue(id) if id.parse::<ulid::Ulid>().is_ok()));
    assert_eq!(
        attribute(
            &collector.capture.metrics.lock().unwrap()[0].resource_metrics[0]
                .resource
                .as_ref()
                .unwrap()
                .attributes,
            "service.instance.id"
        ),
        Some(instance)
    );
    assert_eq!(
        attribute(
            &collector.capture.traces.lock().unwrap()[0].resource_spans[0]
                .resource
                .as_ref()
                .unwrap()
                .attributes,
            "service.instance.id"
        ),
        Some(instance)
    );
    let records = &received.message.resource_logs[0].scope_logs[0].log_records;
    assert_eq!(records.len(), 5);
    for (name, target, starter, count, outcome) in [
        ("dst.server.ban", "KU_TARGET", None, 3, "completed"),
        (
            "dst.server.start_vote",
            "KU_NOMINEE",
            Some("KU_STARTER"),
            2,
            "failure",
        ),
    ] {
        let span = spans.iter().find(|span| span.name == name).unwrap();
        let target = AnyValue::StringValue(target.into());
        let starter = starter.map(|id| AnyValue::StringValue(id.into()));
        assert_eq!(
            attribute(&span.attributes, "dst.operation.target_userid"),
            Some(&target)
        );
        assert_eq!(
            attribute(&span.attributes, "dst.operation.starter_userid"),
            starter.as_ref()
        );
        let linked: Vec<_> = records
            .iter()
            .filter(|record| record.trace_id == span.trace_id && record.span_id == span.span_id)
            .collect();
        assert_eq!(linked.len(), count);
        let mut logged_outcome = None;
        for record in linked
            .into_iter()
            .filter(|record| record.event_name == "dst.server.operation")
        {
            let Some(AnyValue::KvlistValue(body)) =
                record.body.as_ref().and_then(|body| body.value.as_ref())
            else {
                panic!("missing operation body");
            };
            assert_eq!(attribute(&body.values, "target_userid"), Some(&target));
            assert_eq!(attribute(&body.values, "starter_userid"), starter.as_ref());
            if let Some(outcome) = attribute(&body.values, "outcome") {
                logged_outcome = Some(outcome);
            }
        }
        assert_eq!(logged_outcome, Some(&AnyValue::StringValue(outcome.into())));
        assert_eq!(
            attribute(&span.attributes, "dst.operation.outcome"),
            logged_outcome
        );
    }
    let native_uid = |record: &opentelemetry_proto::tonic::logs::v1::LogRecord| {
        attribute(&record.attributes, "log.record.uid").cloned()
    };
    assert_ne!(native_uid(&records[0]), native_uid(&records[1]));
    let Some(Data::Histogram(duration)) =
        &collector.latest_metrics()["dst.server.operation.duration"].data
    else {
        panic!("missing duration")
    };
    assert_eq!(
        duration
            .data_points
            .iter()
            .map(|point| point.count)
            .sum::<u64>(),
        2
    );
    assert!(!format!("{duration:?}").contains("KU_"));
    assert!(
        duration.data_points.iter().all(|point| attribute(
            &point.attributes,
            "dst.operation.outcome"
        )
        .is_none())
    );
    for name in [
        "dst.telemetry.delivery.count",
        "dst.telemetry.pending.bytes",
        "dst.telemetry.pending.records",
    ] {
        assert!(collector.latest_metrics().contains_key(name), "{name}");
    }
    let observer = Observability::new(&collector.config()?)?;
    collector
        .capture
        .metric_replies
        .lock()
        .unwrap()
        .push_back(Err(Status::permission_denied("PRIVATE_SDK_ERROR")));
    collector
        .capture
        .trace_replies
        .lock()
        .unwrap()
        .push_back(Err(Status::permission_denied("PRIVATE_SDK_ERROR")));
    observer
        .begin_operation("cluster", None, "save", None)
        .finish(None);
    assert!(observer.flush().await.is_err());
    collector
        .capture
        .metric_replies
        .lock()
        .unwrap()
        .push_back(Ok(ExportMetricsServiceResponse {
            partial_success: Some(ExportMetricsPartialSuccess {
                rejected_data_points: 1,
                error_message: "PRIVATE_PARTIAL".into(),
            }),
        }));
    collector
        .capture
        .trace_replies
        .lock()
        .unwrap()
        .push_back(Ok(ExportTraceServiceResponse {
            partial_success: Some(ExportTracePartialSuccess {
                rejected_spans: 1,
                error_message: "PRIVATE_PARTIAL".into(),
            }),
        }));
    observer
        .begin_operation("cluster", None, "save", None)
        .finish(None);
    observer.flush().await?;
    observer.flush().await?;
    let metrics = collector.latest_metrics();
    let Some(Data::Sum(calls)) = &metrics["dst.telemetry.exporter.call.count"].data else {
        panic!("missing export results")
    };
    for signal in ["metrics", "traces"] {
        for reason in ["internal_failure", "export_call_success"] {
            assert!(calls.data_points.iter().any(|point| attribute(
                &point.attributes,
                "dst.telemetry.signal"
            ) == Some(&AnyValue::StringValue(
                signal.into()
            )) && attribute(
                &point.attributes,
                "dst.telemetry.reason"
            ) == Some(&AnyValue::StringValue(
                reason.into()
            ))));
        }
    }
    assert!(!format!("{metrics:?}").contains("PRIVATE_"));
    assert!(!format!("{:?}", collector.spans()).contains("PRIVATE_"));
    assert!(metrics.contains_key("dst.telemetry.exporter.transition.count"));
    observer.close().await?;
    logs.stop().await?;
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
