//! Typed OTLP logs. Empty protobuf values preserve JSON nulls.

pub(crate) mod health;
mod identity;
pub(crate) mod local;
pub(crate) mod management;

use std::{
    io::Read,
    os::unix::fs::OpenOptionsExt,
    path::Path,
    sync::{Arc, Mutex},
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
pub(crate) const MAX_SPAN_QUEUE_SIZE: usize = 65_536;

/// The event fields already recorded by the Python SDK.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LogEvent {
    pub event_name: String,
    pub body: Value,
    pub observed_timestamp_ns: u64,
    pub severity_text: String,
    #[serde(default)]
    pub attributes: Map<String, Value>,
    #[serde(flatten, default, skip_serializing_if = "Option::is_none")]
    pub trace: Option<LogTrace>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LogTrace {
    pub trace_id: String,
    pub span_id: String,
    pub trace_flags: u8,
}

#[derive(Serialize)]
struct LocalRecord<'a> {
    #[serde(flatten)]
    log: &'a LogEvent,
    resource: &'a Map<String, Value>,
}

impl LogEvent {
    pub(crate) fn new(event_name: &str, body: Value, severity: &str) -> Self {
        Self {
            event_name: event_name.into(),
            body,
            severity_text: severity.into(),
            observed_timestamp_ns: crate::operation::timestamp(),
            attributes: Map::from_iter([(
                "log.record.uid".into(),
                json!(ulid::Ulid::generate().to_string()),
            )]),
            trace: None,
        }
    }

    pub(crate) fn correlate(&mut self, context: &crate::operation::OperationContext) {
        self.attributes
            .insert("dst.operation.id".into(), json!(context.id));
        if let Some(parent) = &context.parent_id {
            self.attributes
                .insert("dst.operation.parent_id".into(), json!(parent));
        }
        self.attributes
            .insert("dst.operation.source".into(), json!(context.source));
        self.attributes
            .insert("dst.operation.layer".into(), json!(context.layer.as_str()));
        self.trace = context
            .span
            .as_ref()
            .filter(|span| span.is_valid())
            .map(|span| LogTrace {
                trace_id: span.trace_id().to_string(),
                span_id: span.span_id().to_string(),
                trace_flags: span.trace_flags().to_u8(),
            });
    }
    /// Convert once before delivering the observation to local and remote sinks.
    pub fn from_driver(event: crate::driver::DriverEvent, cluster: &str) -> Result<Self> {
        use crate::driver::DriverEventKind;
        let mut attributes = Map::from_iter([
            ("log.record.uid".into(), event.id.into()),
            ("dst.cluster.name".into(), cluster.into()),
            ("dst.shard.name".into(), event.shard.into()),
        ]);
        if let Some(generation) = event.generation {
            attributes.insert("dst.runtime.generation".into(), generation.into());
        }
        let operational = |prefix: &str, data: Value| -> Result<_> {
            let name = data["event"]
                .as_str()
                .context("missing operational event name")?;
            let severity = if name == "process_exited" {
                let exit = &data["exit"];
                if exit["returncode"] != 0
                    || exit["output_drained"] != true
                    || !exit["protocol_error"].is_null()
                    || !data["control_error"].is_null()
                {
                    "ERROR"
                } else if exit["forced"] == true {
                    "WARN"
                } else {
                    "INFO"
                }
            } else {
                "INFO"
            };
            Ok((format!("{prefix}.{name}"), data, severity))
        };
        let (event_name, body, severity) = match event.kind {
            DriverEventKind::Telemetry(game) => {
                ensure!(
                    game.nonce == event.nonce && Some(game.generation.0) == event.generation,
                    "telemetry observation identity does not match its source"
                );
                attributes.insert("dst.event.sequence".into(), game.seq.0.into());
                attributes.insert("dst.tick".into(), game.tick.0.into());
                attributes.insert("dst.monotonic_ms".into(), game.monotonic_ms.0.into());
                if let Some(session) = game.session_id {
                    attributes.insert("dst.session.id".into(), session.0.into());
                }
                if let Some(cycle) = game.cycle {
                    attributes.insert("dst.world.cycle".into(), cycle.0.into());
                }
                (game.body.name().into(), game.body.into_data(), "INFO")
            }
            DriverEventKind::Lifecycle(data) => operational("dst.server", data)?,
            DriverEventKind::Control(data) => operational("dst.control", data)?,
            DriverEventKind::Diagnostic(data) => ("dst.runtime.diagnostic".into(), data, "ERROR"),
            DriverEventKind::Log(data) => {
                let severity = if data["stream"] == "stderr" {
                    "WARN"
                } else {
                    "INFO"
                };
                ("dst.server.log".into(), data, severity)
            }
        };
        attributes.insert("dst.game.attempt.id".into(), event.nonce.into());
        Ok(Self {
            event_name,
            body,
            observed_timestamp_ns: event.observed_timestamp_ns,
            severity_text: severity.into(),
            attributes,
            trace: None,
        })
    }

    pub fn to_otlp(&self) -> Result<LogRecord> {
        ensure!(!self.event_name.is_empty(), "event_name cannot be empty");
        let severity_text = self.severity_text.to_ascii_uppercase();
        let severity = SeverityNumber::from_str_name(&format!("SEVERITY_NUMBER_{severity_text}"))
            .context("unknown OpenTelemetry severity")?;
        let (trace_id, span_id, flags) = if let Some(trace) = &self.trace {
            let trace_id = opentelemetry::trace::TraceId::from_hex(&trace.trace_id)?;
            let span_id = opentelemetry::trace::SpanId::from_hex(&trace.span_id)?;
            ensure!(
                trace_id != opentelemetry::trace::TraceId::INVALID
                    && span_id != opentelemetry::trace::SpanId::INVALID,
                "invalid log trace identity"
            );
            (
                trace_id.to_bytes().to_vec(),
                span_id.to_bytes().to_vec(),
                u32::from(trace.trace_flags),
            )
        } else {
            (Vec::new(), Vec::new(), 0)
        };
        Ok(LogRecord {
            event_name: self.event_name.clone(),
            body: Some(any_value(&self.body).context("invalid log body")?),
            // Lua gameplay occurrence times are relative to the game process.
            time_unix_nano: if self.attributes.contains_key("dst.event.sequence") {
                0
            } else {
                self.observed_timestamp_ns
            },
            observed_time_unix_nano: self.observed_timestamp_ns,
            severity_number: severity as i32,
            severity_text,
            attributes: attributes(&self.attributes, 0).context("invalid log attributes")?,
            trace_id,
            span_id,
            flags,
            ..Default::default()
        })
    }
}

/// Reserve the complete static envelope before accepting a room configuration.
pub(crate) fn validate_context<'a>(
    resource: &Map<String, Value>,
    cluster: &str,
    shards: impl IntoIterator<Item = &'a str>,
) -> crate::model::Result<()> {
    let invalid = || {
        crate::model::Error::new(
            crate::model::ErrorCode::Overflow,
            "telemetry resource or room context exceeds its size limit",
        )
    };
    let template = export_request(resource, &[]).map_err(|_| invalid())?;
    if template.encoded_len().saturating_add(16) > 64 * 1024 {
        return Err(invalid());
    }
    for shard in shards {
        let log = LogEvent {
            trace: None,
            event_name: "x".repeat(128),
            body: Value::Null,
            observed_timestamp_ns: u64::MAX,
            severity_text: "ERROR".into(),
            attributes: json!({
                "dst.cluster.name":cluster,"dst.shard.name":shard,
                "log.record.uid":"7ZZZZZZZZZZZZZZZZZZZZZZZZZ",
                "dst.game.attempt.id":"7ZZZZZZZZZZZZZZZZZZZZZZZZZ",
                "dst.room.attempt.id":"7ZZZZZZZZZZZZZZZZZZZZZZZZZ",
                "dst.operation.id":"7ZZZZZZZZZZZZZZZZZZZZZZZZZ",
                "dst.operation.parent_id":"7ZZZZZZZZZZZZZZZZZZZZZZZZZ",
                "dst.operation.source":"agent_shutdown","dst.operation.layer":"request",
                "dst.runtime.generation":u64::MAX,"dst.event.sequence":u64::MAX,
                "dst.tick":u64::MAX,"dst.monotonic_ms":u64::MAX,
                "dst.session.id":"🌲".repeat(128),"dst.world.cycle":u64::MAX,
            })
            .as_object()
            .unwrap()
            .clone(),
        };
        let mut value = serde_json::to_value(LocalRecord {
            log: &log,
            resource,
        })
        .map_err(|_| invalid())?;
        // Native trace fields are optional; reserve their longest serialized form too.
        value["trace_id"] = json!("f".repeat(32));
        value["span_id"] = json!("f".repeat(16));
        value["trace_flags"] = json!(255);
        crate::rpc::validate_static_context(&value).map_err(|_| invalid())?;
    }
    Ok(())
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

/// Queue bounds include the active batch, measured in encoded log record bytes.
/// Resource attributes are held once; each record is limited to 1 MiB.
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

impl ExportConfig {
    pub(crate) fn validate_batch(&self, max_capacity: usize) -> Result<()> {
        ensure!(
            (1..=max_capacity).contains(&self.queue_capacity),
            "OTLP queue capacity is outside the supported range"
        );
        ensure!(
            (1..=self.queue_capacity).contains(&self.max_batch_records),
            "OTLP batch size must be positive and bounded by its queue"
        );
        Ok(())
    }

    pub(crate) fn channel(&self) -> Result<Channel> {
        let mut endpoint = Endpoint::from_shared(self.endpoint.clone())?
            .connect_timeout(self.export_timeout)
            .timeout(self.export_timeout)
            .buffer_size(1)
            .concurrency_limit(1);
        match endpoint.uri().scheme_str() {
            Some("https") => {
                endpoint = endpoint.tls_config(
                    self.tls
                        .clone()
                        .unwrap_or_else(|| ClientTlsConfig::new().with_native_roots()),
                )?;
            }
            Some("http") => ensure!(
                self.tls.is_none(),
                "TLS configuration requires an https endpoint"
            ),
            _ => bail!("OTLP endpoint must use http or https"),
        }
        Ok(endpoint.connect_lazy())
    }
}

/// Export settings and static resource attributes for one telemetry configuration.
/// Headers and TLS private keys deliberately have no Debug representation here.
#[derive(Default)]
pub struct TelemetryConfig {
    pub logs: Option<ExportConfig>,
    pub metrics: Option<ExportConfig>,
    pub traces: Option<ExportConfig>,
    pub resource: Map<String, Value>,
}

#[derive(Clone, Copy)]
pub(crate) enum ExportSetup {
    Strict,
    Room,
}

impl ExportSetup {
    pub(crate) fn check<T>(self, name: &str, result: Result<Option<T>>) -> Result<Option<T>> {
        match self {
            Self::Strict => result,
            Self::Room => Ok(room_export(name, result)),
        }
    }
}

pub(crate) fn room_export<T>(name: &str, result: Result<Option<T>>) -> Option<T> {
    result.unwrap_or_else(|error| {
        eprintln!("dst-server: OTLP {name} disabled: {error}");
        None
    })
}

impl TelemetryConfig {
    pub fn from_env() -> Result<Self> {
        Self::from_lookup(|name| std::env::var(name).ok())
    }

    /// An explicit lookup keeps configuration tests independent of process env.
    pub fn from_lookup(lookup: impl Fn(&str) -> Option<String>) -> Result<Self> {
        Self::read(lookup, ExportSetup::Strict)
    }

    pub(crate) fn for_agent(lookup: impl Fn(&str) -> Option<String>) -> Result<Self> {
        let mut config = Self::read(&lookup, ExportSetup::Room)?;
        if config.configured_room_name().is_none()
            && let Some(unit) = lookup(crate::deployment::SYSTEMD_UNIT_ENV)
            && let Some(name) = unit
                .strip_suffix(".service")
                .filter(|name| !name.is_empty())
        {
            config
                .resource
                .insert("dst.cluster.name".into(), json!(name));
        }
        Ok(config)
    }

    fn read(lookup: impl Fn(&str) -> Option<String>, setup: ExportSetup) -> Result<Self> {
        let get = |name: &str| lookup(name).filter(|value| !value.is_empty());
        let mut resource = Map::new();
        let attributes = get("OTEL_RESOURCE_ATTRIBUTES")
            .map(|v| environment_pairs(&v))
            .transpose();
        if let Some(pairs) = setup.check("resource attributes", attributes)? {
            for (key, value) in pairs {
                resource.insert(key, Value::String(value));
            }
        }
        if let Some(value) = get("OTEL_SERVICE_NAME") {
            resource.insert("service.name".into(), Value::String(value));
        }
        resource.remove("service.instance.id");
        identity::apply(&mut resource, None);
        let signal = |name| setup.check(name, exporter_settings(&get, name));
        Ok(Self {
            logs: signal("LOGS")?,
            metrics: signal("METRICS")?,
            traces: signal("TRACES")?,
            resource,
        })
    }

    fn configured_room_name(&self) -> Option<&str> {
        self.resource
            .get("dst.cluster.name")
            .and_then(Value::as_str)
            .filter(|name| !name.trim().is_empty())
    }

    pub(crate) fn bind_room(&mut self, executable: &Path, directory: &Path) -> String {
        let room = self
            .configured_room_name()
            .map(str::to_owned)
            .unwrap_or_else(|| {
                directory
                    .file_name()
                    .unwrap_or_default()
                    .to_string_lossy()
                    .into_owned()
            });
        identity::apply(&mut self.resource, Some(executable));
        self.resource.insert(
            "service.instance.id".into(),
            json!(ulid::Ulid::generate().to_string()),
        );
        self.resource.insert("dst.cluster.name".into(), json!(room));
        room
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
    let mut config = ExportConfig {
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
        config.endpoint = endpoint;
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
            config.headers.insert(key, value);
        }
    }
    if let Some(value) = setting("COMPRESSION") {
        ensure!(
            value.eq_ignore_ascii_case("none") || value.eq_ignore_ascii_case("gzip"),
            "OTLP compression must be gzip or none"
        );
        config.gzip = value.eq_ignore_ascii_case("gzip");
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
    config.export_timeout =
        milliseconds(setting("TIMEOUT"), "OTLP timeout", config.export_timeout)?;
    let prefix = if signal == "TRACES" { "BSP" } else { "BLRP" };
    let budget_name = if signal == "METRICS" {
        "OTEL_METRIC_EXPORT_TIMEOUT".into()
    } else {
        format!("OTEL_{prefix}_EXPORT_TIMEOUT")
    };
    if let Some(value) = get(&budget_name) {
        config.export_timeout = config.export_timeout.min(milliseconds(
            Some(value),
            &budget_name,
            config.export_timeout,
        )?);
    }
    let interval_name = if signal == "METRICS" {
        "OTEL_METRIC_EXPORT_INTERVAL".into()
    } else {
        format!("OTEL_{prefix}_SCHEDULE_DELAY")
    };
    config.schedule_delay =
        milliseconds(get(&interval_name), &interval_name, config.schedule_delay)?;
    if signal != "METRICS" {
        for (name, target) in [
            (
                format!("OTEL_{prefix}_MAX_QUEUE_SIZE"),
                &mut config.queue_capacity,
            ),
            (
                format!("OTEL_{prefix}_MAX_EXPORT_BATCH_SIZE"),
                &mut config.max_batch_records,
            ),
        ] {
            if let Some(value) = get(&name) {
                *target = value
                    .parse()
                    .map_err(|_| anyhow::anyhow!("{name} must be a positive integer"))?;
                ensure!(*target > 0, "{name} must be positive");
            }
        }
        config.validate_batch(if signal == "TRACES" {
            MAX_SPAN_QUEUE_SIZE
        } else {
            tokio::sync::Semaphore::MAX_PERMITS
        })?;
    }
    let certificate = setting("CERTIFICATE");
    let client_certificate = setting("CLIENT_CERTIFICATE");
    let client_key = setting("CLIENT_KEY");
    ensure!(
        client_certificate.is_some() == client_key.is_some(),
        "OTLP client certificate and key must be configured together"
    );
    if config.endpoint.starts_with("https://") {
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
        config.tls = Some(tls);
    } else {
        ensure!(
            certificate.is_none() && client_certificate.is_none(),
            "OTLP certificate configuration requires an https endpoint"
        );
    }
    Ok(Some(config))
}

fn read_tls_file(path: &str) -> Result<Vec<u8>> {
    let unreadable = || anyhow::anyhow!("could not read an OTLP TLS credential file");
    let mut file = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NONBLOCK)
        .open(path)
        .map_err(|_| unreadable())?;
    ensure!(
        file.metadata().is_ok_and(|meta| meta.is_file()),
        "OTLP TLS credentials must be regular files"
    );
    let mut bytes = Vec::new();
    file.read_to_end(&mut bytes).map_err(|_| unreadable())?;
    Ok(bytes)
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
struct ExportState {
    stats: Mutex<ExportStats>,
    health: health::ExporterHealth,
}

pub struct LogExporter {
    sender: Mutex<Option<mpsc::Sender<PendingLog>>>,
    control: watch::Sender<ExportControl>,
    progress: watch::Receiver<ExportProgress>,
    shared: Arc<ExportState>,
    config: ExportConfig,
    envelope_bytes: usize,
}

impl LogExporter {
    /// Starts without contacting the collector; collector availability never
    /// prevents the game from starting. Configuration errors are returned here.
    pub fn new(config: ExportConfig, resource: &Map<String, Value>) -> Result<Self> {
        Self::with_local(config, resource, None)
    }

    pub(crate) fn with_local(
        config: ExportConfig,
        resource: &Map<String, Value>,
        local: Option<local::LocalLog>,
    ) -> Result<Self> {
        config.validate_batch(tokio::sync::Semaphore::MAX_PERMITS)?;
        ensure!(
            config.queue_bytes > 0,
            "OTLP queue byte limit must be positive"
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
        let mut client = LogsServiceClient::new(config.channel()?)
            .max_encoding_message_size(config.max_batch_bytes)
            .max_decoding_message_size(4 * 1024 * 1024)
            .accept_compressed(CompressionEncoding::Gzip);
        if config.gzip {
            client = client.send_compressed(CompressionEncoding::Gzip);
        }
        let (sender, receiver) = mpsc::channel(config.queue_capacity);
        let (control, control_rx) = watch::channel(ExportControl::default());
        let (progress_tx, progress) = watch::channel(ExportProgress::default());
        let shared = Arc::new(ExportState {
            stats: Mutex::new(ExportStats::default()),
            health: health::ExporterHealth::new(local),
        });
        runtime.spawn(export_worker(
            client,
            template,
            config.clone(),
            receiver,
            control_rx,
            progress_tx,
            shared.clone(),
        ));
        Ok(Self {
            sender: Mutex::new(Some(sender)),
            control,
            progress,
            shared,
            config,
            envelope_bytes,
        })
    }

    /// Never waits for queue space or network I/O. A full queue drops the new
    /// record; accepted records retain their order and delivery budget.
    pub fn submit(&self, event: &LogEvent) -> Result<Submission> {
        let record = event.to_otlp();
        // Publish local health transitions in admission order across producers.
        let sender = self.sender.lock().unwrap();
        let record = match record {
            Ok(record) => record,
            Err(error) => {
                self.shared.stats.lock().unwrap().invalid += 1;
                self.shared
                    .health
                    .observe("logs", "admission", "invalid", None, true, 1);
                return Err(error);
            }
        };
        let result = self.admit(sender.as_ref(), record);
        let reason = match result {
            Submission::Accepted => "accepted",
            Submission::QueueFull => "queue_full",
            Submission::ByteLimit => "byte_full",
            Submission::TooLarge => "too_large",
            Submission::Closed => "closed",
        };
        self.shared.health.observe(
            "logs",
            "admission",
            reason,
            None,
            result != Submission::Accepted,
            1,
        );
        Ok(result)
    }

    fn admit(&self, sender: Option<&mpsc::Sender<PendingLog>>, record: LogRecord) -> Submission {
        let bytes = record.encoded_len();
        let mut stats = self.shared.stats.lock().unwrap();
        let Some(sender) = sender else {
            stats.closed_dropped += 1;
            return Submission::Closed;
        };
        if bytes > 1024 * 1024
            || bytes.saturating_add(self.envelope_bytes + 8) > self.config.max_batch_bytes
        {
            stats.too_large += 1;
            return Submission::TooLarge;
        }
        if stats.pending_records >= self.config.queue_capacity {
            stats.queue_full += 1;
            return Submission::QueueFull;
        }
        if bytes > self.config.queue_bytes.saturating_sub(stats.pending_bytes) {
            stats.queue_bytes_full += 1;
            return Submission::ByteLimit;
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
                Submission::Accepted
            }
            Err(mpsc::error::TrySendError::Full(_)) => {
                stats.queue_full += 1;
                Submission::QueueFull
            }
            Err(mpsc::error::TrySendError::Closed(_)) => {
                stats.closed_dropped += 1;
                Submission::Closed
            }
        }
    }

    pub(crate) fn health_counters(
        &self,
    ) -> (
        std::collections::BTreeMap<health::HealthKey, u64>,
        std::collections::BTreeMap<health::HealthKey, u64>,
    ) {
        self.shared.health.counters()
    }

    pub fn stats(&self) -> ExportStats {
        self.shared.stats.lock().unwrap().clone()
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
        let mut stats = self.shared.stats.lock().unwrap();
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
    shared: Arc<ExportState>,
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
            let result = send_batch(&mut client, &request, &config, &mut control, &shared).await;
            {
                let mut stats = shared.stats.lock().unwrap();
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
    let (completed, unsettled) = {
        let mut stats = shared.stats.lock().unwrap();
        let unsettled = stats.pending_records as u64;
        stats.shutdown_dropped += unsettled;
        stats.pending_records = 0;
        stats.pending_bytes = 0;
        stats.closed = true;
        (stats.accepted, unsettled)
    };
    shared.health.observe(
        "logs",
        "shutdown",
        if unsettled > 0 {
            "unsettled"
        } else {
            "completed"
        },
        None,
        unsettled > 0,
        unsettled,
    );
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
    shared: &ExportState,
) -> BatchResult {
    let deadline = Instant::now() + config.export_timeout;
    let export = retry_batch(client, request, &config.headers, shared);
    tokio::pin!(export);
    let mut control_open = true;
    loop {
        let deadline = control
            .borrow_and_update()
            .close_deadline
            .map_or(deadline, |close| close.min(deadline));
        tokio::select! {
            biased;
            _ = tokio::time::sleep_until(deadline) => {
                shared.health.observe("logs", "export", "confirmation_timeout", None, true, request.resource_logs[0].scope_logs[0].log_records.len() as u64);
                return BatchResult::TimedOut;
            },
            result = &mut export => return result,
            changed = control.changed(), if control_open => control_open = changed.is_ok(),
        }
    }
}

async fn retry_batch(
    client: &mut LogsServiceClient<Channel>,
    batch: &ExportLogsServiceRequest,
    headers: &MetadataMap,
    shared: &ExportState,
) -> BatchResult {
    let mut backoff = Duration::from_millis(100);
    let mut retrying = false;
    loop {
        if retrying {
            shared.stats.lock().unwrap().retries += 1;
        }
        let mut request = Request::new(batch.clone());
        *request.metadata_mut() = headers.clone();
        match client.export(request).await {
            Ok(response) => {
                let partial = response.into_inner().partial_success.unwrap_or_default();
                let count = batch.resource_logs[0].scope_logs[0].log_records.len() as i64;
                if !(0..=count).contains(&partial.rejected_log_records) {
                    shared.health.observe(
                        "logs",
                        "export",
                        "invalid_response",
                        Some(0),
                        true,
                        count as u64,
                    );
                    return BatchResult::Failed(
                        "collector returned an invalid rejected record count".into(),
                    );
                }
                shared.health.observe(
                    "logs",
                    "export",
                    if partial.rejected_log_records > 0 {
                        "partial_rejection"
                    } else if !partial.error_message.is_empty() {
                        "partial_warning"
                    } else {
                        "acknowledged"
                    },
                    Some(0),
                    partial.rejected_log_records > 0 || !partial.error_message.is_empty(),
                    partial.rejected_log_records as u64,
                );
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
                shared.health.observe(
                    "logs",
                    "export",
                    if temporary {
                        "retry"
                    } else {
                        "permanent_error"
                    },
                    Some(status.code() as i32),
                    true,
                    batch.resource_logs[0].scope_logs[0].log_records.len() as u64,
                );
                if !temporary {
                    return BatchResult::Failed(error);
                }
                shared.stats.lock().unwrap().last_error = Some(error);
                let retry_after = retry_info
                    .and_then(|info| info.retry_delay)
                    .unwrap_or_default();
                // Spread retries even when a collector gives several rooms the same minimum wait.
                let jittered_backoff = backoff.mul_f64(0.5 + fastrand::f64() * 0.5);
                tokio::time::sleep(retry_after.saturating_add(jittered_backoff)).await;
                backoff = backoff.saturating_mul(2).min(Duration::from_secs(5));
                retrying = true;
            }
        }
    }
}
