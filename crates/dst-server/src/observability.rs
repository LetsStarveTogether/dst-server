//! Metrics and operation spans use the official OpenTelemetry SDK and exporters.

use std::{
    collections::{BTreeMap, BTreeSet},
    sync::Mutex,
    time::{Duration, Instant},
};

use anyhow::{Context, Result, anyhow, ensure};
use opentelemetry::{
    KeyValue,
    metrics::{Counter, Histogram, MeterProvider, UpDownCounter, noop::NoopMeterProvider},
    trace::{Span, Status, Tracer, TracerProvider},
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
    driver::{DriverEvent, DriverEventKind, DriverState},
    model,
    telemetry::{ExportConfig, TelemetryConfig},
};

const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(30);
type Shutdown = Option<std::result::Result<(), String>>;

pub struct Observability {
    metrics: Option<SdkMeterProvider>,
    traces: Option<SdkTracerProvider>,
    tracer: Option<SdkTracer>,
    operation_duration: Histogram<f64>,
    process_count: UpDownCounter<i64>,
    player_count: UpDownCounter<i64>,
    client_count: UpDownCounter<i64>,
    telemetry_event_count: Counter<u64>,
    player_action_count: Counter<u64>,
    shards: Mutex<BTreeMap<(String, String), ShardMetrics>>,
    shutdown: Mutex<Option<watch::Receiver<Shutdown>>>,
}

#[derive(Default)]
struct ShardMetrics {
    nonce: String,
    generation: Option<u64>,
    process_up: bool,
    players: BTreeMap<u64, String>,
    clients: BTreeSet<String>,
    player_count: i64,
    client_count: i64,
    gaps: u64,
    invalid: u64,
    stale: u64,
}

fn base_attributes(cluster: &str, shard: &str) -> Vec<KeyValue> {
    vec![
        KeyValue::new("dst.cluster.name", cluster.to_owned()),
        KeyValue::new("dst.shard.name", shard.to_owned()),
    ]
}

fn resource(config: &TelemetryConfig) -> Result<Resource> {
    let attributes = config
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
    Ok(Resource::builder_empty()
        .with_attributes(attributes)
        .build())
}

fn metadata(config: &ExportConfig) -> impl tonic::service::Interceptor + Clone + use<> {
    let headers = config.headers.clone();
    move |mut request: tonic::Request<()>| {
        // Keep the resolved configuration authoritative over ambient SDK headers.
        *request.metadata_mut() = headers.clone();
        Ok(request)
    }
}

fn metric_exporter(config: &ExportConfig) -> Result<opentelemetry_otlp::MetricExporter> {
    let mut exporter = opentelemetry_otlp::MetricExporter::builder()
        .with_tonic()
        .with_endpoint(&config.endpoint)
        .with_timeout(config.export_timeout)
        .with_interceptor(metadata(config));
    if let Some(tls) = &config.tls {
        exporter = exporter.with_tls_config(tls.clone());
    }
    if config.gzip {
        exporter = exporter.with_compression(Compression::Gzip);
    }
    exporter
        .build()
        .map_err(|_| anyhow!("could not configure the OTLP metrics exporter"))
}

fn span_exporter(config: &ExportConfig) -> Result<opentelemetry_otlp::SpanExporter> {
    let mut exporter = opentelemetry_otlp::SpanExporter::builder()
        .with_tonic()
        .with_endpoint(&config.endpoint)
        .with_timeout(config.export_timeout)
        .with_interceptor(metadata(config));
    if let Some(tls) = &config.tls {
        exporter = exporter.with_tls_config(tls.clone());
    }
    if config.gzip {
        exporter = exporter.with_compression(Compression::Gzip);
    }
    exporter
        .build()
        .map_err(|_| anyhow!("could not configure the OTLP traces exporter"))
}

impl Observability {
    pub fn new(config: &TelemetryConfig) -> Result<Self> {
        for settings in [&config.metrics, &config.traces].into_iter().flatten() {
            ensure!(
                (1..=65_536).contains(&settings.queue_capacity),
                "SDK queue capacity is outside the supported range"
            );
            ensure!(
                settings.max_batch_records > 0
                    && settings.max_batch_records <= settings.queue_capacity,
                "SDK batch size must be positive and bounded by its queue"
            );
            for duration in [settings.schedule_delay, settings.export_timeout] {
                ensure!(
                    !duration.is_zero() && Instant::now().checked_add(duration).is_some(),
                    "SDK timing is outside the supported range"
                );
            }
        }
        if config.metrics.is_some() || config.traces.is_some() {
            tokio::runtime::Handle::try_current()
                .context("OTLP SDK exporters need a Tokio runtime")?;
        }
        let resource = resource(config)?;
        // Construct both exporters before providers, so failed setup starts no workers.
        let metric_exporter = config.metrics.as_ref().map(metric_exporter).transpose()?;
        let span_exporter = config.traces.as_ref().map(span_exporter).transpose()?;
        let metrics = metric_exporter.map(|exporter| {
            let reader = PeriodicReader::builder(exporter)
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
                    BatchSpanProcessor::builder(exporter)
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
        Ok(Self {
            operation_duration: meter
                .f64_histogram("dst.server.operation.duration")
                .with_unit("s")
                .with_description("Duration of DST server SDK operations.")
                .build(),
            process_count: meter
                .i64_up_down_counter("dst.server.process.count")
                .with_unit("{process}")
                .build(),
            player_count: meter
                .i64_up_down_counter("dst.server.player.count")
                .with_unit("{player}")
                .build(),
            client_count: meter
                .i64_up_down_counter("dst.server.client.count")
                .with_unit("{client}")
                .build(),
            telemetry_event_count: meter
                .u64_counter("dst.telemetry.event.count")
                .with_unit("{event}")
                .build(),
            player_action_count: meter
                .u64_counter("dst.player.action.count")
                .with_unit("{action}")
                .build(),
            metrics,
            traces,
            tracer,
            shards: Mutex::new(BTreeMap::new()),
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
        let mut attributes = vec![
            KeyValue::new("dst.cluster.name", cluster.to_owned()),
            KeyValue::new("dst.operation.name", method.to_owned()),
        ];
        if let Some(shard) = shard {
            attributes.push(KeyValue::new("dst.shard.name", shard.to_owned()));
        }
        let span = self.tracer.as_ref().map(|tracer| {
            let mut span_attributes = attributes.clone();
            if let Some(session) = session {
                span_attributes.push(KeyValue::new("dst.session.id", session.to_owned()));
            }
            tracer.build(
                tracer
                    .span_builder(format!("dst.server.{method}"))
                    .with_attributes(span_attributes),
            )
        });
        OperationObservation {
            span,
            duration: self.operation_duration.clone(),
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
        if state.nonce != source.nonce {
            self.reset_presence(state, &attributes);
            state.nonce = source.nonce.clone();
            state.generation = source.generation;
            state.gaps = 0;
            state.invalid = 0;
            state.stale = 0;
        } else if source.generation > state.generation {
            self.reset_presence(state, &attributes);
            state.generation = source.generation;
        }
        if state.process_up != source.running {
            self.process_count
                .add(if source.running { 1 } else { -1 }, &attributes);
            state.process_up = source.running;
        }
        for (total, previous, outcome, reason) in [
            (source.telemetry_gaps, &mut state.gaps, "gap", "sequence"),
            (
                source.invalid_telemetry,
                &mut state.invalid,
                "invalid",
                "invalid_record",
            ),
            (
                source.stale_telemetry,
                &mut state.stale,
                "ignored",
                "stale_sequence_or_generation",
            ),
        ] {
            let count = total.saturating_sub(*previous);
            if count > 0 {
                self.record_event(&attributes, outcome, Some(reason), None, count);
            }
            *previous = (*previous).max(total);
        }
        if !source.running {
            self.reset_presence(state, &attributes);
        }
    }

    pub fn observe_driver(&self, source: &DriverEvent, cluster: &str) {
        if self.metrics.is_none() {
            return;
        }
        if !matches!(source.kind, DriverEventKind::Telemetry) {
            return;
        }
        let Some(name) = source.data["event"].as_str() else {
            return;
        };
        let data = &source.data["data"];
        let attributes = base_attributes(cluster, &source.shard);
        let mut shards = self.shards.lock().unwrap();
        let state = shards
            .entry((cluster.into(), source.shard.clone()))
            .or_default();
        if state.nonce.is_empty() {
            state.nonce = source.nonce.clone();
        }
        if state.nonce != source.nonce || source.generation < state.generation {
            return;
        }
        if source.generation > state.generation {
            self.reset_presence(state, &attributes);
            state.generation = source.generation;
        }
        self.record_event(&attributes, "accepted", None, Some(name), 1);
        if name == "dst.player.action"
            && let (Some(action), Some(success)) =
                (data["action_id"].as_str(), data["success"].as_bool())
        {
            let mut action_attributes = attributes.clone();
            action_attributes.extend([
                KeyValue::new("dst.action.name", action.to_owned()),
                KeyValue::new("dst.action.success", success),
            ]);
            self.player_action_count.add(1, &action_attributes);
        }
        if !state.process_up {
            return;
        }
        match name {
            "dst.server.presence" => {
                if let (Some(players), Some(clients)) =
                    (data["players"].as_array(), data["clients"].as_array())
                {
                    state.players = players
                        .iter()
                        .filter_map(|player| {
                            Some((
                                player["guid"].as_u64()?,
                                player["userid"].as_str()?.to_owned(),
                            ))
                        })
                        .collect();
                    state.clients = clients
                        .iter()
                        .filter_map(|client| client.as_str().map(str::to_owned))
                        .collect();
                }
            }
            "dst.player.shard_entered" | "dst.player.loaded" => {
                if let (Some(guid), Some(userid)) = (
                    data["player"]["guid"].as_u64(),
                    data["player"]["userid"].as_str(),
                ) {
                    state.players.insert(guid, userid.into());
                }
            }
            "dst.player.shard_left" => {
                if let Some(guid) = data["player"]["guid"].as_u64() {
                    state.players.remove(&guid);
                }
            }
            "dst.client.authenticated" => {
                if let Some(userid) = data["userid"].as_str() {
                    state.clients.insert(userid.into());
                }
            }
            "dst.client.disconnected" => {
                if let Some(userid) = data["userid"].as_str() {
                    state.clients.remove(userid);
                    state.players.retain(|_, value| value != userid);
                }
            }
            _ => (),
        }
        self.update_presence(state, &attributes);
    }

    pub fn record_subscription_drop(&self, cluster: &str, shard: &str, count: u64) {
        if count > 0 {
            self.record_event(
                &base_attributes(cluster, shard),
                "dropped",
                Some("driver_subscription"),
                None,
                count,
            );
        }
    }

    fn record_event(
        &self,
        attributes: &[KeyValue],
        outcome: &str,
        reason: Option<&str>,
        event: Option<&str>,
        count: u64,
    ) {
        let mut attributes = attributes.to_vec();
        attributes.push(KeyValue::new("dst.telemetry.outcome", outcome.to_owned()));
        if let Some(reason) = reason {
            attributes.push(KeyValue::new("dst.telemetry.reason", reason.to_owned()));
        }
        if let Some(event) = event {
            attributes.push(KeyValue::new("dst.event.name", event.to_owned()));
        }
        self.telemetry_event_count.add(count, &attributes);
    }

    fn update_presence(&self, state: &mut ShardMetrics, attributes: &[KeyValue]) {
        let players = state.players.values().collect::<BTreeSet<_>>().len() as i64;
        let clients = state.clients.len() as i64;
        if players != state.player_count {
            self.player_count
                .add(players - state.player_count, attributes);
            state.player_count = players;
        }
        if clients != state.client_count {
            self.client_count
                .add(clients - state.client_count, attributes);
            state.client_count = clients;
        }
    }

    fn reset_presence(&self, state: &mut ShardMetrics, attributes: &[KeyValue]) {
        state.players.clear();
        state.clients.clear();
        self.update_presence(state, attributes);
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
    duration: Histogram<f64>,
    attributes: Vec<KeyValue>,
    started: Instant,
    finished: bool,
}

impl OperationObservation {
    pub fn finish(mut self, error: Option<&model::Error>) {
        let error_type = error.map(|error| {
            serde_json::to_value(error.code)
                .unwrap()
                .as_str()
                .unwrap()
                .to_owned()
        });
        self.complete(error_type.as_deref());
    }

    fn complete(&mut self, error_type: Option<&str>) {
        if let Some(error_type) = error_type {
            let attribute = KeyValue::new("error.type", error_type.to_owned());
            self.attributes.push(attribute.clone());
            if let Some(span) = &mut self.span {
                span.set_attribute(attribute);
                span.set_status(Status::error(error_type.to_owned()));
            }
        }
        self.duration
            .record(self.started.elapsed().as_secs_f64(), &self.attributes);
        if let Some(span) = &mut self.span {
            span.end();
        }
        self.finished = true;
    }
}

impl Drop for OperationObservation {
    fn drop(&mut self) {
        if !self.finished {
            self.complete(Some(if std::thread::panicking() {
                "panic"
            } else {
                "cancelled"
            }));
        }
    }
}
