//! Metrics and operation spans use the official OpenTelemetry SDK and exporters.

mod exporters;
mod outputs;

use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex},
    time::{Duration, Instant},
};

use anyhow::{Context, Result, anyhow, ensure};
use opentelemetry::{
    KeyValue,
    metrics::{Counter, Histogram, MeterProvider, UpDownCounter, noop::NoopMeterProvider},
    trace::{Span, Status, TraceContextExt, Tracer, TracerProvider},
};
use opentelemetry_otlp::{Compression, WithExportConfig, WithTonicConfig};
use opentelemetry_sdk::{
    Resource,
    metrics::{PeriodicReader, SdkMeterProvider},
    trace::{BatchConfigBuilder, BatchSpanProcessor, SdkTracer, SdkTracerProvider},
};
use serde_json::Value;
use tokio::sync::watch;

use crate::{
    driver::DriverState,
    model, policy,
    telemetry::{ExportConfig, ExportSetup, MAX_SPAN_QUEUE_SIZE, TelemetryConfig},
};

const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(30);
type Shutdown = Option<std::result::Result<(), String>>;
type Shards = Arc<Mutex<BTreeMap<(String, String), ShardMetrics>>>;

pub struct Observability {
    metrics: Option<SdkMeterProvider>,
    traces: Option<SdkTracerProvider>,
    tracer: Option<SdkTracer>,
    operation_duration: Histogram<f64>,
    process_count: UpDownCounter<i64>,
    telemetry_event_count: Counter<u64>,
    sequence_gap_count: Counter<u64>,
    shards: Shards,
    shutdown: Mutex<Option<watch::Receiver<Shutdown>>>,
}

#[derive(Default)]
struct ShardMetrics {
    process_id: String,
    ready_session: Option<String>,
    generation: Option<u64>,
    process_up: bool,
    presence: Option<PresenceSample>,
    gaps: u64,
    relay_dropped: u64,
    rejections: BTreeMap<crate::events::RejectionReason, u64>,
}

struct PresenceSample {
    observed_at: Instant,
    observed_unix_seconds: u64,
    connection_count: u64,
    entity_count: u64,
}

fn base_attributes(cluster: &str, shard: &str) -> [KeyValue; 2] {
    [
        KeyValue::new("dst.cluster.name", cluster.to_owned()),
        KeyValue::new("dst.shard.name", shard.to_owned()),
    ]
}

fn resource(config: &TelemetryConfig) -> Result<Resource> {
    let mut attributes = config
        .resource
        .iter()
        .map(|(key, value)| {
            let value: opentelemetry::Value = match value {
                Value::String(value) => value.clone().into(),
                Value::Bool(value) => (*value).into(),
                Value::Number(value) if value.is_i64() => value.as_i64().unwrap().into(),
                Value::Number(value) if value.is_f64() => value
                    .as_f64()
                    .filter(|value| value.is_finite())
                    .context("SDK resource numbers must be finite")?
                    .into(),
                _ => {
                    return Err(anyhow!(
                        "SDK resource attributes must be strings, booleans or signed numeric values"
                    ));
                }
            };
            Ok(KeyValue::new(key.clone(), value))
        })
        .collect::<Result<Vec<_>>>()?;
    if !config.resource.contains_key("service.instance.id") {
        attributes.push(KeyValue::new(
            "service.instance.id",
            ulid::Ulid::generate().to_string(),
        ));
    }
    Ok(Resource::builder_empty()
        .with_attributes(attributes)
        .build())
}

fn sdk_transport<T: WithTonicConfig + WithExportConfig>(
    builder: T,
    config: &ExportConfig,
) -> Result<T> {
    for duration in [config.schedule_delay, config.export_timeout] {
        ensure!(
            !duration.is_zero() && Instant::now().checked_add(duration).is_some(),
            "SDK timing is outside the supported range"
        );
    }
    tokio::runtime::Handle::try_current().context("OTLP SDK exporters need a Tokio runtime")?;
    let headers = config.headers.clone();
    let mut builder = builder
        .with_channel(config.channel()?)
        .with_timeout(config.export_timeout)
        .with_interceptor(move |mut request: tonic::Request<()>| {
            // Keep the resolved configuration authoritative over ambient SDK headers.
            *request.metadata_mut() = headers.clone();
            Ok(request)
        });
    if config.gzip {
        builder = builder.with_compression(Compression::Gzip);
    }
    Ok(builder)
}

impl Observability {
    pub fn new(config: &TelemetryConfig) -> Result<Self> {
        Self::build(config, None)
    }

    pub(crate) fn for_room(
        config: &TelemetryConfig,
        local: crate::telemetry::local::LocalLog,
        exporter: Option<Arc<crate::telemetry::LogExporter>>,
    ) -> Option<Self> {
        if config.metrics.is_none() && config.traces.is_none() {
            return None;
        }
        let observer = Self::build(config, Some((local, exporter))).map(Some);
        crate::telemetry::room_export("METRICS/TRACES", observer)
            .filter(|observer| observer.metrics.is_some() || observer.traces.is_some())
    }

    fn build(config: &TelemetryConfig, outputs: outputs::Outputs) -> Result<Self> {
        let setup = if outputs.is_some() {
            ExportSetup::Room
        } else {
            ExportSetup::Strict
        };
        let resource = resource(config)?;
        // Construct both exporters before providers, so failed setup starts no workers.
        let metric_exporter = setup.check(
            "METRICS",
            config
                .metrics
                .as_ref()
                .map(|settings| {
                    sdk_transport(
                        opentelemetry_otlp::MetricExporter::builder().with_tonic(),
                        settings,
                    )?
                    .build()
                    .map_err(|_| anyhow!("could not configure the OTLP metrics exporter"))
                })
                .transpose(),
        )?;
        let span_exporter = setup.check(
            "TRACES",
            config
                .traces
                .as_ref()
                .map(|settings| {
                    settings.validate_batch(MAX_SPAN_QUEUE_SIZE)?;
                    sdk_transport(
                        opentelemetry_otlp::SpanExporter::builder().with_tonic(),
                        settings,
                    )?
                    .build()
                    .map_err(|_| anyhow!("could not configure the OTLP traces exporter"))
                })
                .transpose(),
        )?;
        let health = Arc::new(crate::telemetry::health::ExporterHealth::new(
            outputs.as_ref().map(|(local, _)| local.clone()),
        ));
        let metrics = metric_exporter.map(|exporter| {
            let reader = PeriodicReader::builder(exporters::Metrics {
                inner: exporter,
                health: health.clone(),
            })
            .with_interval(config.metrics.as_ref().unwrap().schedule_delay)
            .build();
            SdkMeterProvider::builder()
                .with_resource(resource.clone())
                .with_reader(reader)
                .build()
        });
        let traces = span_exporter.map(|exporter| {
            let settings = config.traces.as_ref().unwrap();
            let batch = BatchConfigBuilder::default()
                .with_max_queue_size(settings.queue_capacity)
                .with_max_export_batch_size(settings.max_batch_records)
                .with_scheduled_delay(settings.schedule_delay)
                .build();
            SdkTracerProvider::builder()
                .with_resource(resource)
                .with_span_processor(
                    BatchSpanProcessor::builder(exporters::Traces {
                        inner: exporter,
                        health: health.clone(),
                    })
                    .with_batch_config(batch)
                    .build(),
                )
                .build()
        });
        let meter = metrics.as_ref().map_or_else(
            || NoopMeterProvider::new().meter("dst-server"),
            |provider| provider.meter("dst-server"),
        );
        let tracer = traces
            .as_ref()
            .map(|provider| provider.tracer("dst-server"));
        let shards = Shards::default();
        outputs::register(&meter, outputs, health, shards.clone());
        let presence_gauge = |name, unit, description, value: fn(&PresenceSample) -> u64| {
            let shards = shards.clone();
            meter
                .u64_observable_gauge(name)
                .with_unit(unit)
                .with_description(description)
                .with_callback(move |observer| {
                    for ((cluster, shard), state) in shards.lock().unwrap().iter() {
                        // A busy room can skip a scheduled poll; expire its last reading.
                        if let Some(sample) = &state.presence
                            && sample.observed_at.elapsed() <= policy::CHECK_INTERVAL * 2
                        {
                            observer.observe(value(sample), &base_attributes(cluster, shard));
                        }
                    }
                })
                .build();
        };
        presence_gauge(
            "dst.server.client.connection.count",
            "{connection}",
            "Connected client entries in the shard.",
            |sample| sample.connection_count,
        );
        presence_gauge(
            "dst.server.player.entity.count",
            "{entity}",
            "Player entities with an account in the shard, including characters still loading.",
            |sample| sample.entity_count,
        );
        presence_gauge(
            "dst.server.presence.last_observed",
            "s",
            "Unix time when the Agent received the reliable shard occupancy sample.",
            |sample| sample.observed_unix_seconds,
        );
        Ok(Self {
            operation_duration: meter
                .f64_histogram("dst.server.operation.duration")
                .with_unit("s")
                .with_description("Duration of DST server SDK operations.")
                .with_boundaries(vec![
                    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0,
                    120.0, 300.0,
                ])
                .build(),
            process_count: meter
                .i64_up_down_counter("dst.server.process.count")
                .with_unit("{process}")
                .build(),
            telemetry_event_count: meter
                .u64_counter("dst.telemetry.event.count")
                .with_unit("{event}")
                .with_description(
                    "Gameplay records received by the room relay or rejected by the driver.",
                )
                .build(),
            sequence_gap_count: meter
                .u64_counter("dst.telemetry.sequence.gap.count")
                .with_unit("{record}")
                .with_description(
                    "Missing sequence numbers before or between accepted Lua records.",
                )
                .build(),
            metrics,
            traces,
            tracer,
            shards,
            shutdown: Mutex::new(None),
        })
    }

    pub fn begin_operation(
        &self,
        cluster: &str,
        shard: Option<&str>,
        method: &str,
        session: Option<&str>,
    ) -> OperationObservation {
        let mut context = crate::operation::OperationContext::new(
            crate::operation::Source::RoomApi,
            crate::operation::Layer::Room,
            None,
        );
        self.begin_context_operation(cluster, shard, method, session, &mut context)
    }

    pub(crate) fn begin_context_operation(
        &self,
        cluster: &str,
        shard: Option<&str>,
        method: &str,
        session: Option<&str>,
        context: &mut crate::operation::OperationContext,
    ) -> OperationObservation {
        let mut attributes = vec![
            KeyValue::new("dst.cluster.name", cluster.to_owned()),
            KeyValue::new("dst.operation.name", method.to_owned()),
        ];
        if let Some(shard) = shard {
            attributes.push(KeyValue::new("dst.shard.name", shard.to_owned()));
        }
        let span = self.tracer.as_ref().map(|tracer| {
            let mut span_attributes = attributes.clone();
            span_attributes.extend([
                KeyValue::new("dst.operation.id", context.id.clone()),
                KeyValue::new("dst.operation.source", context.source.as_str()),
                KeyValue::new("dst.operation.layer", context.layer.as_str()),
            ]);
            if let Some(parent) = &context.parent_id {
                span_attributes.push(KeyValue::new("dst.operation.parent_id", parent.clone()));
            }
            if let Some(session) = session {
                span_attributes.push(KeyValue::new("dst.session.id", session.to_owned()));
            }
            for (role, userid) in &context.player_roles {
                span_attributes.push(KeyValue::new(
                    format!("dst.operation.{role}"),
                    userid.clone(),
                ));
            }
            let parent = context
                .span
                .as_ref()
                .filter(|span| span.is_valid())
                .map_or_else(opentelemetry::Context::new, |span| {
                    opentelemetry::Context::new().with_remote_span_context(span.clone())
                });
            tracer.build_with_context(
                tracer
                    .span_builder(format!("dst.server.{method}"))
                    .with_attributes(span_attributes),
                &parent,
            )
        });
        context.span = span
            .as_ref()
            .map(|span| span.span_context().clone())
            .filter(|span| span.is_valid());
        OperationObservation {
            span,
            duration: matches!(context.layer, crate::operation::Layer::Room)
                .then(|| self.operation_duration.clone()),
            attributes,
            started: Instant::now(),
            finished: false,
        }
    }

    /// Called from the state watch, independent of the bounded event subscription.
    pub fn observe_state(&self, source: &DriverState, cluster: &str) {
        if self.metrics.is_none() {
            return;
        }
        let attributes = base_attributes(cluster, &source.shard);
        let mut shards = self.shards.lock().unwrap();
        let state = shards
            .entry((cluster.into(), source.shard.clone()))
            .or_default();
        if state.process_id != source.process_id {
            state.presence = None;
            state.process_id = source.process_id.clone();
            state.gaps = 0;
            state.rejections.clear();
        }
        let ready_session = source
            .session_id
            .as_deref()
            .filter(|_| source.running && source.ready);
        if source.generation != state.generation || state.ready_session.as_deref() != ready_session
        {
            state.presence = None;
            state.generation = source.generation;
            state.ready_session = ready_session.map(str::to_owned);
        }
        if state.process_up != source.running {
            self.process_count
                .add(if source.running { 1 } else { -1 }, &attributes);
            state.process_up = source.running;
        }
        if source.telemetry_gaps > state.gaps {
            self.sequence_gap_count
                .add(source.telemetry_gaps - state.gaps, &attributes);
            state.gaps = source.telemetry_gaps;
        }
        for (reason, total) in &source.telemetry_rejections {
            let previous = state.rejections.entry(*reason).or_default();
            let count = total.saturating_sub(*previous);
            if count > 0 {
                let Value::String(reason) = serde_json::json!(reason) else {
                    unreachable!("rejection reasons serialize as enum labels")
                };
                self.record_event(
                    attributes.clone(),
                    "rejected",
                    KeyValue::new("dst.telemetry.reason", reason),
                    count,
                );
            }
            *previous = (*previous).max(*total);
        }
    }

    /// Use the control poll already made by the room policy, including in the off profile.
    pub fn observe_presence(
        &self,
        cluster: &str,
        shard: &str,
        identity: &model::SessionIdentity,
        presence: Option<&policy::Presence>,
    ) {
        if self.metrics.is_none() {
            return;
        }
        let mut shards = self.shards.lock().unwrap();
        let Some(state) = shards.get_mut(&(cluster.to_owned(), shard.to_owned())) else {
            return;
        };
        if state.process_id == identity.process_id
            && state.generation == Some(identity.generation)
            && state.ready_session.as_deref() == Some(identity.session_id.as_str())
        {
            state.presence = presence
                .filter(|reading| reading.reliable && reading.session_id == identity.session_id)
                .map(|reading| PresenceSample {
                    observed_at: Instant::now(),
                    observed_unix_seconds: crate::operation::timestamp() / 1_000_000_000,
                    connection_count: reading.client_count,
                    entity_count: reading.player_count,
                });
        }
    }

    pub fn observe_gameplay(&self, cluster: &str, shard: &str, name: &'static str) {
        if self.metrics.is_some() {
            self.record_event(
                base_attributes(cluster, shard),
                "accepted",
                KeyValue::new("dst.event.name", name),
                1,
            );
        }
    }

    pub fn record_relay_drop(&self, cluster: &str, shard: &str, count: u64) {
        if count > 0 && self.metrics.is_some() {
            let mut shards = self.shards.lock().unwrap();
            let state = shards.entry((cluster.into(), shard.into())).or_default();
            state.relay_dropped = state.relay_dropped.saturating_add(count);
        }
    }

    fn record_event(
        &self,
        [cluster, shard]: [KeyValue; 2],
        outcome: &'static str,
        detail: KeyValue,
        count: u64,
    ) {
        self.telemetry_event_count.add(
            count,
            &[
                cluster,
                shard,
                KeyValue::new("dst.telemetry.outcome", outcome),
                detail,
            ],
        );
    }

    pub async fn flush(&self) -> Result<()> {
        let metrics = self.metrics.clone();
        let traces = self.traces.clone();
        tokio::task::spawn_blocking(move || {
            let metrics = metrics.map(|provider| provider.force_flush()).transpose();
            let traces = traces.map(|provider| provider.force_flush()).transpose();
            metrics.context("metrics flush failed")?;
            traces.context("traces flush failed")?;
            Ok(())
        })
        .await
        .context("SDK flush worker failed")?
    }

    /// Provider shutdown survives cancellation of the caller waiting for it.
    pub async fn close(&self) -> Result<()> {
        let mut completion = {
            let mut shutdown = self.shutdown.lock().unwrap();
            shutdown
                .get_or_insert_with(|| {
                    let (sender, receiver) = watch::channel(None);
                    let metrics = self.metrics.clone();
                    let traces = self.traces.clone();
                    tokio::spawn(async move {
                        let metric_task = tokio::task::spawn_blocking(move || {
                            metrics
                                .map(|provider| provider.shutdown_with_timeout(SHUTDOWN_TIMEOUT))
                                .transpose()
                        });
                        let trace_task = tokio::task::spawn_blocking(move || {
                            traces
                                .map(|provider| provider.shutdown_with_timeout(SHUTDOWN_TIMEOUT))
                                .transpose()
                        });
                        let (metrics, traces) = tokio::join!(metric_task, trace_task);
                        let result = (|| -> Result<()> {
                            metrics
                                .context("metrics shutdown worker failed")?
                                .context("metrics shutdown failed")?;
                            traces
                                .context("traces shutdown worker failed")?
                                .context("traces shutdown failed")?;
                            Ok(())
                        })()
                        .map_err(|error| error.to_string());
                        sender.send_replace(Some(result));
                    });
                    receiver
                })
                .clone()
        };
        let result = tokio::time::timeout(SHUTDOWN_TIMEOUT, completion.wait_for(Option::is_some))
            .await
            .context("SDK shutdown exceeded 30 seconds")?
            .context("SDK shutdown worker stopped")?
            .clone()
            .unwrap();
        result.map_err(anyhow::Error::msg)
    }
}

pub struct OperationObservation {
    span: Option<opentelemetry_sdk::trace::Span>,
    duration: Option<Histogram<f64>>,
    attributes: Vec<KeyValue>,
    started: Instant,
    finished: bool,
}

impl OperationObservation {
    pub fn finish(self, error: Option<&model::Error>) {
        self.finish_with(error.map_or(Ok("completed"), Err).into());
    }

    pub(crate) fn finish_with(mut self, completion: crate::operation::OperationCompletion<'_>) {
        let error_type = completion.error.map(|error| {
            serde_json::to_value(error.code)
                .unwrap()
                .as_str()
                .unwrap()
                .to_owned()
        });
        self.complete(completion.outcome, error_type.as_deref());
    }

    fn complete(&mut self, outcome: &'static str, error_type: Option<&str>) {
        if let Some(error_type) = error_type {
            let attribute = KeyValue::new("error.type", error_type.to_owned());
            self.attributes.push(attribute.clone());
            if let Some(span) = &mut self.span {
                span.set_attribute(attribute);
                span.set_status(Status::error(error_type.to_owned()));
            }
        }
        if let Some(duration) = &self.duration {
            duration.record(self.started.elapsed().as_secs_f64(), &self.attributes);
        }
        if let Some(span) = &mut self.span {
            span.set_attribute(KeyValue::new("dst.operation.outcome", outcome));
            span.end();
        }
        self.finished = true;
    }
}

impl Drop for OperationObservation {
    fn drop(&mut self) {
        if !self.finished {
            let outcome = if std::thread::panicking() {
                "panic"
            } else {
                "cancelled"
            };
            self.complete(outcome, Some(outcome));
        }
    }
}
