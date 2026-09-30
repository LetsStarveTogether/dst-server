//! Typed OTLP logs. Empty protobuf values preserve JSON nulls.

use std::{
    sync::{
        Arc, Mutex,
        atomic::{AtomicU64, Ordering},
    },
    time::Duration,
};

use anyhow::{Context, Result, bail, ensure};
use opentelemetry_proto::tonic::{
    collector::logs::v1::{ExportLogsServiceRequest, logs_service_client::LogsServiceClient},
    common::v1::{AnyValue, ArrayValue, InstrumentationScope, KeyValue, KeyValueList, any_value},
    logs::v1::{LogRecord, ResourceLogs, ScopeLogs, SeverityNumber},
    resource::v1::Resource,
};
use prost::Message;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};
use tokio::{
    sync::{mpsc, watch},
    time::{Instant, MissedTickBehavior},
};
use tonic::{
    Code, Request,
    codec::CompressionEncoding,
    metadata::{MetadataKey, MetadataMap, MetadataValue},
    transport::{Certificate, Channel, ClientTlsConfig, Endpoint, Identity},
};
use tonic_types::StatusExt;

// Each JSON object adds three protobuf message levels. Leave room for the
// enclosing log/resource messages within prost's default recursion limit.
const MAX_VALUE_DEPTH: usize = 24;

/// The event fields already recorded by the Python SDK.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LogEvent {
    pub event_name: String,
    pub body: Value,
    pub observed_timestamp_ns: u64,
    pub severity_text: String,
    #[serde(default)]
    pub attributes: Map<String, Value>,
}

impl LogEvent {
    /// Convert once before delivering the observation to local and remote sinks.
    pub fn from_driver(event: &crate::driver::DriverEvent, cluster: &str) -> Result<Self> {
        use crate::driver::DriverEventKind;
        let mut attributes = Map::from_iter([
            ("dst.cluster.name".into(), json!(cluster)),
            ("dst.shard.name".into(), json!(event.shard)),
            ("dst.game.attempt.id".into(), json!(event.nonce)),
        ]);
        if let Some(generation) = event.generation {
            attributes.insert("dst.runtime.generation".into(), json!(generation));
        }
        let (event_name, body, severity, uid) = match event.kind {
            DriverEventKind::Telemetry => {
                let record = &event.data;
                let generation = record["generation"]
                    .as_u64()
                    .context("missing telemetry generation")?;
                let sequence = record["seq"]
                    .as_u64()
                    .filter(|value| *value > 0)
                    .context("missing telemetry sequence")?;
                ensure!(
                    record["nonce"] == event.nonce && event.generation == Some(generation),
                    "telemetry observation identity does not match its source"
                );
                let name = record["event"]
                    .as_str()
                    .filter(|name| name.starts_with("dst.") && name.len() <= 128)
                    .context("invalid telemetry event name")?;
                ensure!(
                    record["data"].is_object(),
                    "telemetry body must be an object"
                );
                attributes.insert("dst.event.sequence".into(), json!(sequence));
                for (field, attribute) in
                    [("tick", "dst.tick"), ("monotonic_ms", "dst.monotonic_ms")]
                {
                    let value = record[field]
                        .as_u64()
                        .context("missing telemetry source clock")?;
                    attributes.insert(attribute.into(), json!(value));
                }
                for (field, attribute) in [
                    ("session_id", "dst.session.id"),
                    ("cycle", "dst.world.cycle"),
                ] {
                    if let Some(value) = record.get(field).filter(|value| !value.is_null()) {
                        attributes.insert(attribute.into(), value.clone());
                    }
                }
                (
                    name.to_owned(),
                    record["data"].clone(),
                    if name == "dst.telemetry.error" {
                        "ERROR"
                    } else {
                        "INFO"
                    },
                    format!("{}:{generation}:{sequence}", event.nonce),
                )
            }
            kind => {
                let (name, severity) = match kind {
                    DriverEventKind::Lifecycle | DriverEventKind::Control => {
                        let name = event.data["event"]
                            .as_str()
                            .context("missing operational event name")?;
                        let prefix = if matches!(kind, DriverEventKind::Lifecycle) {
                            "dst.server"
                        } else {
                            "dst.control"
                        };
                        (format!("{prefix}.{name}"), "INFO")
                    }
                    DriverEventKind::Diagnostic => ("dst.runtime.diagnostic".into(), "ERROR"),
                    DriverEventKind::Log => (
                        "dst.server.log".into(),
                        if event.data["stream"] == "stderr" {
                            "WARN"
                        } else {
                            "INFO"
                        },
                    ),
                    DriverEventKind::Telemetry => unreachable!(),
                };
                (
                    name,
                    event.data.clone(),
                    severity,
                    ulid::Ulid::new().to_string(),
                )
            }
        };
        attributes.insert("log.record.uid".into(), Value::String(uid));
        Ok(Self {
            event_name,
            body,
            observed_timestamp_ns: event.observed_timestamp_ns,
            severity_text: severity.into(),
            attributes,
        })
    }

    pub fn to_otlp(&self) -> Result<LogRecord> {
        ensure!(!self.event_name.is_empty(), "event_name cannot be empty");
        let severity_text = self.severity_text.to_ascii_uppercase();
        let severity = SeverityNumber::from_str_name(&format!("SEVERITY_NUMBER_{severity_text}"))
            .context("unknown OpenTelemetry severity")?;
        Ok(LogRecord {
            event_name: self.event_name.clone(),
            body: Some(any_value(&self.body).context("invalid log body")?),
            time_unix_nano: self.observed_timestamp_ns,
            observed_time_unix_nano: self.observed_timestamp_ns,
            severity_number: severity as i32,
            severity_text,
            attributes: attributes(&self.attributes, 0).context("invalid log attributes")?,
            ..Default::default()
        })
    }
}

/// Build one batch for a single resource without changing event or value types.
pub fn export_request(
    resource_attributes: &Map<String, Value>,
    events: &[LogEvent],
) -> Result<ExportLogsServiceRequest> {
    Ok(ExportLogsServiceRequest {
        resource_logs: vec![ResourceLogs {
            resource: Some(Resource {
                attributes: attributes(resource_attributes, 0)
                    .context("invalid resource attributes")?,
                ..Default::default()
            }),
            scope_logs: vec![ScopeLogs {
                scope: Some(InstrumentationScope {
                    name: "dst-server".into(),
                    version: env!("CARGO_PKG_VERSION").into(),
                    ..Default::default()
                }),
                log_records: events
                    .iter()
                    .map(LogEvent::to_otlp)
                    .collect::<Result<_>>()?,
                ..Default::default()
            }],
            ..Default::default()
        }],
    })
}

/// Convert JSON directly to protobuf. Integers never pass through a float.
pub fn any_value(value: &Value) -> Result<AnyValue> {
    convert_value(value, 0)
}

fn convert_value(value: &Value, depth: usize) -> Result<AnyValue> {
    ensure!(
        depth <= MAX_VALUE_DEPTH,
        "log value nesting exceeds {MAX_VALUE_DEPTH}"
    );
    use any_value::Value as Proto;
    let value = match value {
        Value::Null => None,
        Value::Bool(value) => Some(Proto::BoolValue(*value)),
        Value::String(value) => Some(Proto::StringValue(value.clone())),
        Value::Number(value) => Some(if let Some(value) = value.as_i64() {
            Proto::IntValue(value)
        } else if value.is_f64() {
            let value = value.as_f64().context("log number must be finite")?;
            ensure!(value.is_finite(), "log number must be finite");
            Proto::DoubleValue(value)
        } else {
            bail!("log number must be a signed 64-bit integer or a finite float");
        }),
        Value::Array(values) => Some(Proto::ArrayValue(ArrayValue {
            values: values
                .iter()
                .map(|value| convert_value(value, depth + 1))
                .collect::<Result<_>>()?,
        })),
        Value::Object(values) => Some(Proto::KvlistValue(KeyValueList {
            values: attributes(values, depth + 1)?,
        })),
    };
    Ok(AnyValue { value })
}

fn attributes(values: &Map<String, Value>, depth: usize) -> Result<Vec<KeyValue>> {
    values
        .iter()
        .map(|(key, value)| {
            Ok(KeyValue {
                key: key.clone(),
                value: Some(convert_value(value, depth).with_context(|| format!("key {key:?}"))?),
                ..Default::default()
            })
        })
        .collect()
}

/// One instance belongs to one source, subscription, or local log sink.
/// Loss at these stages is independent of loss in the OTLP exporter.
#[derive(Debug, Default)]
pub struct StreamLossCounters {
    source_gaps: AtomicU64,
    subscription_dropped: AtomicU64,
    log_dropped: AtomicU64,
}

#[derive(Debug, Default, Clone, Copy, Serialize)]
pub struct StreamLossSnapshot {
    pub source_gaps: u64,
    pub subscription_dropped: u64,
    pub log_dropped: u64,
}

impl StreamLossCounters {
    pub fn record_source_gap(&self, count: u64) {
        self.source_gaps.fetch_add(count, Ordering::Relaxed);
    }

    pub fn record_subscription_drop(&self, count: u64) {
        self.subscription_dropped
            .fetch_add(count, Ordering::Relaxed);
    }

    pub fn record_log_drop(&self, count: u64) {
        self.log_dropped.fetch_add(count, Ordering::Relaxed);
    }

    pub fn snapshot(&self) -> StreamLossSnapshot {
        StreamLossSnapshot {
            source_gaps: self.source_gaps.load(Ordering::Relaxed),
            subscription_dropped: self.subscription_dropped.load(Ordering::Relaxed),
            log_dropped: self.log_dropped.load(Ordering::Relaxed),
        }
    }
}

/// Queue bounds include the active batch, measured in encoded log record bytes.
/// Resource attributes are held once; each record is limited to 64 KiB.
#[derive(Clone)]
pub struct ExportConfig {
    pub endpoint: String,
    pub headers: MetadataMap,
    pub tls: Option<ClientTlsConfig>,
    pub gzip: bool,
    pub queue_capacity: usize,
    pub queue_bytes: usize,
    pub max_batch_records: usize,
    pub max_batch_bytes: usize,
    pub schedule_delay: Duration,
    pub export_timeout: Duration,
    pub drain_timeout: Duration,
}

impl Default for ExportConfig {
    fn default() -> Self {
        Self {
            endpoint: "http://localhost:4317".into(),
            headers: MetadataMap::new(),
            tls: None,
            gzip: false,
            queue_capacity: 2048,
            queue_bytes: 16 * 1024 * 1024,
            max_batch_records: 512,
            max_batch_bytes: 3 * 1024 * 1024,
            schedule_delay: Duration::from_secs(1),
            export_timeout: Duration::from_secs(10),
            drain_timeout: Duration::from_secs(30),
        }
    }
}

/// Resolved once per Agent, so every signal can share the same resource identity.
/// Headers and TLS private keys deliberately have no Debug representation here.
pub struct TelemetryConfig {
    pub logs: Option<ExportConfig>,
    pub metrics: Option<ExportConfig>,
    pub traces: Option<ExportConfig>,
    pub resource: Map<String, Value>,
}

impl TelemetryConfig {
    pub fn from_env() -> Result<Self> {
        Self::from_lookup(|name| std::env::var(name).ok())
    }

    /// An explicit lookup keeps configuration tests independent of process env.
    pub fn from_lookup(lookup: impl Fn(&str) -> Option<String>) -> Result<Self> {
        let get = |name: &str| lookup(name).filter(|value| !value.is_empty());
        let mut resource = Map::from_iter([
            ("service.name".into(), json!("dst-server")),
            ("service.version".into(), json!(env!("CARGO_PKG_VERSION"))),
            (
                "service.instance.id".into(),
                json!(ulid::Ulid::new().to_string()),
            ),
        ]);
        if let Some(value) = get("OTEL_RESOURCE_ATTRIBUTES") {
            for (key, value) in environment_pairs(&value)? {
                resource.insert(key, Value::String(value));
            }
        }
        if let Some(value) = get("OTEL_SERVICE_NAME") {
            resource.insert("service.name".into(), Value::String(value));
        }
        Ok(Self {
            logs: exporter_settings(&get, "LOGS")?,
            metrics: exporter_settings(&get, "METRICS")?,
            traces: exporter_settings(&get, "TRACES")?,
            resource,
        })
    }
}

fn exporter_settings(
    get: &impl Fn(&str) -> Option<String>,
    signal: &str,
) -> Result<Option<ExportConfig>> {
    let setting = |suffix: &str| {
        get(&format!("OTEL_EXPORTER_OTLP_{signal}_{suffix}"))
            .or_else(|| get(&format!("OTEL_EXPORTER_OTLP_{suffix}")))
    };
    let enabled = !get("OTEL_SDK_DISABLED").is_some_and(|value| value.eq_ignore_ascii_case("true"));
    let exporter = get(&format!("OTEL_{signal}_EXPORTER")).unwrap_or_else(|| "otlp".into());
    if !enabled || exporter.eq_ignore_ascii_case("none") {
        return Ok(None);
    }
    ensure!(
        exporter.eq_ignore_ascii_case("otlp"),
        "OTEL_{signal}_EXPORTER must be otlp or none"
    );
    ensure!(
        setting("PROTOCOL").is_none_or(|value| value.eq_ignore_ascii_case("grpc")),
        "the {signal} exporter supports the grpc OTLP protocol"
    );
    let mut logs = ExportConfig {
        schedule_delay: Duration::from_secs(match signal {
            "TRACES" => 5,
            "METRICS" => 60,
            _ => 1,
        }),
        ..ExportConfig::default()
    };
    if let Some(endpoint) = setting("ENDPOINT") {
        let endpoint = if endpoint.contains("://") {
            endpoint
        } else {
            let insecure =
                setting("INSECURE").is_some_and(|value| value.eq_ignore_ascii_case("true"));
            format!("{}://{endpoint}", if insecure { "http" } else { "https" })
        };
        let url = reqwest::Url::parse(&endpoint)
            .map_err(|_| anyhow::anyhow!("invalid OTLP endpoint URL"))?;
        ensure!(
            matches!(url.scheme(), "http" | "https")
                && url.host_str().is_some()
                && url.username().is_empty()
                && url.password().is_none()
                && url.query().is_none()
                && url.fragment().is_none(),
            "OTLP endpoint must be an HTTP URL without embedded credentials, query or fragment"
        );
        logs.endpoint = endpoint;
    }
    if let Some(value) = setting("HEADERS") {
        for (key, value) in environment_pairs(&value)? {
            let key: MetadataKey<_> = key
                .parse()
                .map_err(|_| anyhow::anyhow!("invalid OTLP metadata key"))?;
            let mut value: MetadataValue<_> = value
                .parse()
                .map_err(|_| anyhow::anyhow!("invalid OTLP metadata value"))?;
            value.set_sensitive(true);
            logs.headers.insert(key, value);
        }
    }
    if let Some(value) = setting("COMPRESSION") {
        ensure!(
            value.eq_ignore_ascii_case("none") || value.eq_ignore_ascii_case("gzip"),
            "OTLP compression must be gzip or none"
        );
        logs.gzip = value.eq_ignore_ascii_case("gzip");
    }
    let milliseconds = |value: Option<String>, name: &str, default: Duration| -> Result<Duration> {
        value.map_or(Ok(default), |value| {
            let value: u64 = value
                .parse()
                .map_err(|_| anyhow::anyhow!("{name} must be milliseconds"))?;
            ensure!(value > 0, "{name} must be positive");
            Ok(Duration::from_millis(value))
        })
    };
    logs.export_timeout = milliseconds(setting("TIMEOUT"), "OTLP timeout", logs.export_timeout)?;
    let prefix = if signal == "TRACES" { "BSP" } else { "BLRP" };
    let budget_name = if signal == "METRICS" {
        "OTEL_METRIC_EXPORT_TIMEOUT".into()
    } else {
        format!("OTEL_{prefix}_EXPORT_TIMEOUT")
    };
    if let Some(value) = get(&budget_name) {
        logs.export_timeout = logs.export_timeout.min(milliseconds(
            Some(value),
            &budget_name,
            logs.export_timeout,
        )?);
    }
    let interval_name = if signal == "METRICS" {
        "OTEL_METRIC_EXPORT_INTERVAL".into()
    } else {
        format!("OTEL_{prefix}_SCHEDULE_DELAY")
    };
    logs.schedule_delay = milliseconds(get(&interval_name), &interval_name, logs.schedule_delay)?;
    if signal != "METRICS" {
        for (name, target) in [
            (
                format!("OTEL_{prefix}_MAX_QUEUE_SIZE"),
                &mut logs.queue_capacity,
            ),
            (
                format!("OTEL_{prefix}_MAX_EXPORT_BATCH_SIZE"),
                &mut logs.max_batch_records,
            ),
        ] {
            if let Some(value) = get(&name) {
                *target = value
                    .parse()
                    .map_err(|_| anyhow::anyhow!("{name} must be a positive integer"))?;
                ensure!(*target > 0, "{name} must be positive");
            }
        }
    }
    ensure!(
        logs.max_batch_records <= logs.queue_capacity,
        "OTLP batch size must not exceed queue capacity"
    );
    let certificate = setting("CERTIFICATE");
    let client_certificate = setting("CLIENT_CERTIFICATE");
    let client_key = setting("CLIENT_KEY");
    ensure!(
        client_certificate.is_some() == client_key.is_some(),
        "OTLP client certificate and key must be configured together"
    );
    if logs.endpoint.starts_with("https://") {
        let mut tls = ClientTlsConfig::new().with_native_roots();
        if let Some(path) = certificate {
            tls = tls.ca_certificate(Certificate::from_pem(read_tls_file(&path)?));
        }
        if let (Some(certificate), Some(key)) = (client_certificate, client_key) {
            tls = tls.identity(Identity::from_pem(
                read_tls_file(&certificate)?,
                read_tls_file(&key)?,
            ));
        }
        logs.tls = Some(tls);
    } else {
        ensure!(
            certificate.is_none() && client_certificate.is_none(),
            "OTLP certificate configuration requires an https endpoint"
        );
    }
    Ok(Some(logs))
}

fn read_tls_file(path: &str) -> Result<Vec<u8>> {
    std::fs::read(path).map_err(|_| anyhow::anyhow!("could not read an OTLP TLS credential file"))
}

fn environment_pairs(value: &str) -> Result<Vec<(String, String)>> {
    value
        .split(',')
        .map(|pair| {
            let (key, value) = pair
                .trim()
                .split_once('=')
                .context("OTLP environment fields must be key=value pairs")?;
            ensure!(
                !key.trim().is_empty(),
                "OTLP environment field names must be nonempty"
            );
            let value = percent_encoding::percent_decode_str(value.trim())
                .decode_utf8()
                .map_err(|_| anyhow::anyhow!("OTLP environment values must be UTF-8"))?;
            Ok((key.trim().to_owned(), value.into_owned()))
        })
        .collect()
}

#[derive(Debug, Default, Clone, Serialize)]
pub struct ExportStats {
    pub accepted: u64,
    pub exported: u64,
    pub rejected: u64,
    pub failed: u64,
    pub timed_out: u64,
    pub shutdown_dropped: u64,
    pub invalid: u64,
    pub too_large: u64,
    pub queue_full: u64,
    pub queue_bytes_full: u64,
    pub closed_dropped: u64,
    pub retries: u64,
    pub pending_records: usize,
    pub pending_bytes: usize,
    pub closed: bool,
    pub last_error: Option<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Submission {
    Accepted,
    QueueFull,
    ByteLimit,
    TooLarge,
    Closed,
}

struct PendingLog {
    record: LogRecord,
    bytes: usize,
    sequence: u64,
}

#[derive(Debug, Default, Clone, Copy)]
struct ExportControl {
    flush_through: u64,
    close_deadline: Option<Instant>,
}

#[derive(Debug, Default, Clone, Copy)]
struct ExportProgress {
    completed: u64,
    stopped: bool,
}

/// A nonblocking producer and an independently owned export task.
/// Use `Arc<LogExporter>` to share a producer. Call `close` before stopping Tokio.
pub struct LogExporter {
    sender: Mutex<Option<mpsc::Sender<PendingLog>>>,
    control: watch::Sender<ExportControl>,
    progress: watch::Receiver<ExportProgress>,
    stats: Arc<Mutex<ExportStats>>,
    config: ExportConfig,
    envelope_bytes: usize,
}

impl LogExporter {
    /// Starts without contacting the collector; collector availability never
    /// prevents the game from starting. Configuration errors are returned here.
    pub fn new(config: ExportConfig, resource: &Map<String, Value>) -> Result<Self> {
        ensure!(
            (1..=tokio::sync::Semaphore::MAX_PERMITS).contains(&config.queue_capacity),
            "OTLP queue capacity is outside the supported range"
        );
        ensure!(
            config.queue_bytes > 0,
            "OTLP queue byte limit must be positive"
        );
        ensure!(
            config.max_batch_records > 0,
            "OTLP batch size must be positive"
        );
        for (name, duration) in [
            ("schedule delay", config.schedule_delay),
            ("export timeout", config.export_timeout),
            ("drain timeout", config.drain_timeout),
        ] {
            ensure!(
                !duration.is_zero() && Instant::now().checked_add(duration).is_some(),
                "OTLP {name} is outside the supported range"
            );
        }
        let template = export_request(resource, &[])?;
        // Reserve room for growth of the enclosing protobuf length prefixes.
        let envelope_bytes = template.encoded_len() + 16;
        ensure!(envelope_bytes <= 64 * 1024, "OTLP resource exceeds 64 KiB");
        ensure!(
            config.max_batch_bytes > envelope_bytes,
            "OTLP batch byte limit cannot hold its resource"
        );
        let runtime =
            tokio::runtime::Handle::try_current().context("OTLP exporter needs a Tokio runtime")?;
        let mut endpoint = Endpoint::from_shared(config.endpoint.clone())?
            .connect_timeout(config.export_timeout)
            .timeout(config.export_timeout)
            .buffer_size(1)
            .concurrency_limit(1);
        match endpoint.uri().scheme_str() {
            Some("https") => {
                endpoint = endpoint.tls_config(
                    config
                        .tls
                        .clone()
                        .unwrap_or_else(|| ClientTlsConfig::new().with_native_roots()),
                )?;
            }
            Some("http") => ensure!(
                config.tls.is_none(),
                "TLS configuration requires an https endpoint"
            ),
            _ => bail!("OTLP endpoint must use http or https"),
        }
        let mut client = LogsServiceClient::new(endpoint.connect_lazy())
            .max_encoding_message_size(config.max_batch_bytes)
            .max_decoding_message_size(4 * 1024 * 1024)
            .accept_compressed(CompressionEncoding::Gzip);
        if config.gzip {
            client = client.send_compressed(CompressionEncoding::Gzip);
        }
        let (sender, receiver) = mpsc::channel(config.queue_capacity);
        let (control, control_rx) = watch::channel(ExportControl::default());
        let (progress_tx, progress) = watch::channel(ExportProgress::default());
        let stats = Arc::new(Mutex::new(ExportStats::default()));
        runtime.spawn(export_worker(
            client,
            template,
            config.clone(),
            receiver,
            control_rx,
            progress_tx,
            stats.clone(),
        ));
        Ok(Self {
            sender: Mutex::new(Some(sender)),
            control,
            progress,
            stats,
            config,
            envelope_bytes,
        })
    }

    /// Never waits for queue space or network I/O. A full queue drops the new
    /// record; accepted records retain their order and delivery budget.
    pub fn submit(&self, event: &LogEvent) -> Result<Submission> {
        let record = match event.to_otlp() {
            Ok(record) => record,
            Err(error) => {
                self.stats.lock().unwrap().invalid += 1;
                return Err(error);
            }
        };
        let bytes = record.encoded_len();
        let sender = self.sender.lock().unwrap();
        let mut stats = self.stats.lock().unwrap();
        let Some(sender) = sender.as_ref() else {
            stats.closed_dropped += 1;
            return Ok(Submission::Closed);
        };
        if bytes > 64 * 1024
            || bytes.saturating_add(self.envelope_bytes + 8) > self.config.max_batch_bytes
        {
            stats.too_large += 1;
            return Ok(Submission::TooLarge);
        }
        if stats.pending_records >= self.config.queue_capacity {
            stats.queue_full += 1;
            return Ok(Submission::QueueFull);
        }
        if bytes > self.config.queue_bytes.saturating_sub(stats.pending_bytes) {
            stats.queue_bytes_full += 1;
            return Ok(Submission::ByteLimit);
        }
        let pending = PendingLog {
            record,
            bytes,
            sequence: stats.accepted + 1,
        };
        match sender.try_send(pending) {
            Ok(()) => {
                stats.accepted += 1;
                stats.pending_records += 1;
                stats.pending_bytes += bytes;
                Ok(Submission::Accepted)
            }
            Err(mpsc::error::TrySendError::Full(_)) => {
                stats.queue_full += 1;
                Ok(Submission::QueueFull)
            }
            Err(mpsc::error::TrySendError::Closed(_)) => {
                stats.closed_dropped += 1;
                Ok(Submission::Closed)
            }
        }
    }

    pub fn stats(&self) -> ExportStats {
        self.stats.lock().unwrap().clone()
    }

    /// Settles every record accepted before this call. Check the returned loss
    /// counts: completion includes explicit rejection or exhausted retry budget.
    /// Cancellation or a flush timeout only ends the caller's wait.
    pub async fn flush(&self) -> Result<ExportStats> {
        let target = self.stats().accepted;
        self.control
            .send_modify(|control| control.flush_through = control.flush_through.max(target));
        tokio::time::timeout(self.config.drain_timeout, self.wait(target, false))
            .await
            .context("OTLP flush wait timed out; exporter is still running")?
    }

    /// Stops accepting events, then drains within the configured deadline.
    /// Repeated or cancelled calls observe the same independently running close.
    pub async fn close(&self) -> Result<ExportStats> {
        let target = self.begin_close();
        self.wait(target, true).await
    }

    fn begin_close(&self) -> u64 {
        let mut sender = self.sender.lock().unwrap();
        let mut stats = self.stats.lock().unwrap();
        if sender.take().is_some() {
            stats.closed = true;
            self.control.send_modify(|control| {
                control.flush_through = stats.accepted;
                control.close_deadline = Some(Instant::now() + self.config.drain_timeout);
            });
        }
        stats.accepted
    }

    async fn wait(&self, target: u64, stopping: bool) -> Result<ExportStats> {
        let mut progress = self.progress.clone();
        let completed = *progress
            .wait_for(|progress| progress.stopped || (!stopping && progress.completed >= target))
            .await
            .context("OTLP export task stopped unexpectedly")?;
        ensure!(
            completed.completed >= target,
            "OTLP export task left unsettled records"
        );
        Ok(self.stats())
    }
}

impl Drop for LogExporter {
    fn drop(&mut self) {
        self.begin_close();
    }
}

enum BatchResult {
    Delivered {
        rejected: u64,
        warning: Option<String>,
    },
    Failed(String),
    TimedOut,
}

async fn export_worker(
    mut client: LogsServiceClient<Channel>,
    template: ExportLogsServiceRequest,
    config: ExportConfig,
    mut receiver: mpsc::Receiver<PendingLog>,
    mut control: watch::Receiver<ExportControl>,
    progress: watch::Sender<ExportProgress>,
    stats: Arc<Mutex<ExportStats>>,
) {
    let mut interval = tokio::time::interval_at(
        Instant::now() + config.schedule_delay,
        config.schedule_delay,
    );
    interval.set_missed_tick_behavior(MissedTickBehavior::Delay);
    let mut batch = Vec::new();
    let envelope_bytes = template.encoded_len() + 16;
    let mut batch_bytes = envelope_bytes;
    let mut next: Option<PendingLog> = None;
    let mut closed = false;
    let mut control_open = true;
    let mut flush = false;
    let mut completed = 0;
    loop {
        let command = *control.borrow_and_update();
        if command
            .close_deadline
            .is_some_and(|deadline| deadline <= Instant::now())
        {
            break;
        }
        if !batch.is_empty()
            && (flush
                || closed
                || batch.len() >= config.max_batch_records
                || (command.flush_through > completed
                    && batch
                        .last()
                        .is_some_and(|item: &PendingLog| item.sequence >= command.flush_through)))
        {
            let count = batch.len() as u64;
            let bytes = batch.iter().map(|item| item.bytes).sum::<usize>();
            completed = batch.last().unwrap().sequence;
            let mut request = template.clone();
            request.resource_logs[0].scope_logs[0].log_records =
                batch.drain(..).map(|item| item.record).collect();
            let result = send_batch(&mut client, &request, &config, &mut control, &stats).await;
            {
                let mut stats = stats.lock().unwrap();
                stats.pending_records -= count as usize;
                stats.pending_bytes -= bytes;
                match result {
                    BatchResult::Delivered { rejected, warning } => {
                        stats.exported += count - rejected;
                        stats.rejected += rejected;
                        stats.last_error = warning;
                    }
                    BatchResult::Failed(error) => {
                        stats.failed += count;
                        stats.last_error = Some(error);
                    }
                    BatchResult::TimedOut => {
                        stats.timed_out += count;
                        stats.last_error = Some("OTLP batch delivery deadline expired".into());
                    }
                }
            }
            progress.send_replace(ExportProgress {
                completed,
                stopped: false,
            });
            batch_bytes = envelope_bytes;
            flush = false;
            continue;
        }
        if closed && batch.is_empty() {
            break;
        }
        if let Some(item) = next.take() {
            batch_bytes += item.bytes + 8;
            batch.push(item);
            continue;
        }
        tokio::select! {
            item = receiver.recv(), if !closed => match item {
                Some(item) => {
                    if batch_bytes + item.bytes + 8 > config.max_batch_bytes {
                        next = Some(item);
                        flush = true;
                    } else {
                        batch_bytes += item.bytes + 8;
                        batch.push(item);
                    }
                }
                None => closed = true,
            },
            _ = interval.tick() => flush = !batch.is_empty(),
            changed = control.changed(), if control_open => {
                control_open = changed.is_ok();
            },
            _ = tokio::time::sleep_until(command.close_deadline.unwrap_or_else(Instant::now)), if command.close_deadline.is_some() => break,
        }
    }
    receiver.close();
    // Every remaining accepted record is in this receiver, batch, or next slot.
    // Drop them together and report the loss even if the close waiter cancelled.
    drop(receiver);
    drop(batch);
    drop(next);
    let completed = {
        let mut stats = stats.lock().unwrap();
        stats.shutdown_dropped += stats.pending_records as u64;
        stats.pending_records = 0;
        stats.pending_bytes = 0;
        stats.closed = true;
        stats.accepted
    };
    progress.send_replace(ExportProgress {
        completed,
        stopped: true,
    });
}

async fn send_batch(
    client: &mut LogsServiceClient<Channel>,
    request: &ExportLogsServiceRequest,
    config: &ExportConfig,
    control: &mut watch::Receiver<ExportControl>,
    stats: &Mutex<ExportStats>,
) -> BatchResult {
    let deadline = Instant::now() + config.export_timeout;
    let export = retry_batch(client, request, &config.headers, stats);
    tokio::pin!(export);
    let mut control_open = true;
    loop {
        let deadline = control
            .borrow_and_update()
            .close_deadline
            .map_or(deadline, |close| close.min(deadline));
        tokio::select! {
            biased;
            _ = tokio::time::sleep_until(deadline) => return BatchResult::TimedOut,
            result = &mut export => return result,
            changed = control.changed(), if control_open => control_open = changed.is_ok(),
        }
    }
}

async fn retry_batch(
    client: &mut LogsServiceClient<Channel>,
    batch: &ExportLogsServiceRequest,
    headers: &MetadataMap,
    stats: &Mutex<ExportStats>,
) -> BatchResult {
    let mut backoff = Duration::from_millis(100);
    let mut retrying = false;
    loop {
        if retrying {
            stats.lock().unwrap().retries += 1;
        }
        let mut request = Request::new(batch.clone());
        *request.metadata_mut() = headers.clone();
        match client.export(request).await {
            Ok(response) => {
                let Some(partial) = response.into_inner().partial_success else {
                    return BatchResult::Delivered {
                        rejected: 0,
                        warning: None,
                    };
                };
                let count = batch.resource_logs[0].scope_logs[0].log_records.len() as i64;
                if !(0..=count).contains(&partial.rejected_log_records) {
                    return BatchResult::Failed(
                        "collector returned an invalid rejected record count".into(),
                    );
                }
                return BatchResult::Delivered {
                    rejected: partial.rejected_log_records as u64,
                    warning: (!partial.error_message.is_empty())
                        .then(|| partial.error_message.chars().take(1024).collect()),
                };
            }
            Err(status) => {
                let retry_info = status.get_details_retry_info();
                let temporary = matches!(
                    status.code(),
                    Code::Cancelled
                        | Code::DeadlineExceeded
                        | Code::Aborted
                        | Code::OutOfRange
                        | Code::Unavailable
                        | Code::DataLoss
                ) || (status.code() == Code::ResourceExhausted
                    && retry_info.is_some());
                let error: String = format!("{}: {}", status.code(), status.message())
                    .chars()
                    .take(1024)
                    .collect();
                if !temporary {
                    return BatchResult::Failed(error);
                }
                stats.lock().unwrap().last_error = Some(error);
                let delay = retry_info
                    .and_then(|info| info.retry_delay)
                    .unwrap_or_default()
                    .max(backoff);
                tokio::time::sleep(delay).await;
                backoff = backoff.saturating_mul(2).min(Duration::from_secs(5));
                retrying = true;
            }
        }
    }
}
