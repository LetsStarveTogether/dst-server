use std::{collections::BTreeMap, fs, path::Path, process::Command, sync::Arc, time::Duration};

use anyhow::{Context, Result, ensure};
use dst_server::{
    observability::Observability,
    telemetry::{ExportConfig, LogEvent, LogExporter, Submission, TelemetryConfig, export_request},
};
use opentelemetry_proto::tonic::collector::{
    logs::v1::{
        ExportLogsServiceRequest, ExportLogsServiceResponse,
        logs_service_server::{LogsService, LogsServiceServer},
    },
    metrics::v1::{
        ExportMetricsServiceRequest, ExportMetricsServiceResponse,
        metrics_service_server::{MetricsService, MetricsServiceServer},
    },
    trace::v1::{
        ExportTraceServiceRequest, ExportTraceServiceResponse,
        trace_service_server::{TraceService, TraceServiceServer},
    },
};
use serde_json::json;
use tokio::{
    sync::{Semaphore, mpsc},
    task::JoinHandle,
};
use tokio_stream::wrappers::TcpListenerStream;
use tonic::{
    Request, Response, Status,
    transport::{Certificate, Identity, Server, ServerTlsConfig},
};

struct Credentials(tempfile::TempDir);

impl Credentials {
    fn new() -> Result<Self> {
        let directory = tempfile::tempdir()?;
        let run = |arguments: &[&str]| -> Result<()> {
            let output = Command::new("openssl")
                .args(arguments)
                .current_dir(directory.path())
                .output()
                .context("openssl is required for real OTLP TLS tests")?;
            ensure!(
                output.status.success(),
                "test certificate generation failed: {}",
                String::from_utf8_lossy(&output.stderr)
            );
            Ok(())
        };
        run(&[
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:P-256",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=OTLP test CA",
            "-keyout",
            "ca.key",
            "-out",
            "ca.pem",
            "-addext",
            "basicConstraints=critical,CA:TRUE",
        ])?;
        for (name, usage) in [("server", "serverAuth"), ("client", "clientAuth")] {
            let key = format!("{name}.key");
            let csr = format!("{name}.csr");
            let pem = format!("{name}.pem");
            let ext = format!("{name}.ext");
            fs::write(
                directory.path().join(&ext),
                format!(
                    "basicConstraints=critical,CA:FALSE\nextendedKeyUsage={usage}\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n"
                ),
            )?;
            run(&[
                "req",
                "-new",
                "-newkey",
                "ec",
                "-pkeyopt",
                "ec_paramgen_curve:P-256",
                "-nodes",
                "-subj",
                &format!("/CN={name}"),
                "-keyout",
                &key,
                "-out",
                &csr,
            ])?;
            run(&[
                "x509",
                "-req",
                "-in",
                &csr,
                "-CA",
                "ca.pem",
                "-CAkey",
                "ca.key",
                "-CAcreateserial",
                "-days",
                "1",
                "-extfile",
                &ext,
                "-out",
                &pem,
            ])?;
        }
        Ok(Self(directory))
    }
    fn path(&self, name: &str) -> String {
        self.0.path().join(name).to_string_lossy().into_owned()
    }
    fn read(&self, name: &str) -> Result<Vec<u8>> {
        Ok(fs::read(self.0.path().join(name))?)
    }
    fn config(&self, endpoint: &str, client: bool, ca: &Path) -> Result<ExportConfig> {
        let mut environment = BTreeMap::from([
            ("OTEL_LOGS_EXPORTER", "otlp".to_owned()),
            ("OTEL_METRICS_EXPORTER", "none".to_owned()),
            ("OTEL_TRACES_EXPORTER", "none".to_owned()),
            ("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", endpoint.to_owned()),
            (
                "OTEL_EXPORTER_OTLP_LOGS_CERTIFICATE",
                ca.to_string_lossy().into_owned(),
            ),
            (
                "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
                "authorization=Bearer%20tls-secret".to_owned(),
            ),
        ]);
        if client {
            environment.insert(
                "OTEL_EXPORTER_OTLP_LOGS_CLIENT_CERTIFICATE",
                self.path("client.pem"),
            );
            environment.insert(
                "OTEL_EXPORTER_OTLP_LOGS_CLIENT_KEY",
                self.path("client.key"),
            );
        }
        let mut config = TelemetryConfig::from_lookup(|name| environment.get(name).cloned())?
            .logs
            .unwrap();
        config.max_batch_records = 1;
        config.schedule_delay = Duration::from_millis(5);
        config.export_timeout = Duration::from_millis(500);
        config.drain_timeout = Duration::from_secs(2);
        Ok(config)
    }
}

struct Capture {
    received: mpsc::Sender<(bool, String, ExportLogsServiceRequest)>,
    blocked: Arc<Semaphore>,
}

#[derive(Clone)]
struct SdkCapture {
    received: mpsc::Sender<&'static str>,
    mutual: bool,
}

impl SdkCapture {
    fn check<T>(&self, request: &Request<T>) {
        assert_eq!(
            request.peer_certs().is_some_and(|certs| !certs.is_empty()),
            self.mutual
        );
        assert_eq!(
            request.metadata().get("authorization").unwrap(),
            "Bearer tls-secret"
        );
    }
}

#[tonic::async_trait]
impl MetricsService for SdkCapture {
    async fn export(
        &self,
        request: Request<ExportMetricsServiceRequest>,
    ) -> Result<Response<ExportMetricsServiceResponse>, Status> {
        self.check(&request);
        assert!(
            request
                .get_ref()
                .resource_metrics
                .iter()
                .flat_map(|resource| &resource.scope_metrics)
                .flat_map(|scope| &scope.metrics)
                .any(|metric| metric.name == "dst.server.operation.duration")
        );
        self.received.try_send("metrics").unwrap();
        Ok(Response::new(ExportMetricsServiceResponse::default()))
    }
}

#[tonic::async_trait]
impl TraceService for SdkCapture {
    async fn export(
        &self,
        request: Request<ExportTraceServiceRequest>,
    ) -> Result<Response<ExportTraceServiceResponse>, Status> {
        self.check(&request);
        assert!(
            request
                .get_ref()
                .resource_spans
                .iter()
                .flat_map(|resource| &resource.scope_spans)
                .flat_map(|scope| &scope.spans)
                .any(|span| span.name == "dst.server.tlscheck")
        );
        self.received.try_send("traces").unwrap();
        Ok(Response::new(ExportTraceServiceResponse::default()))
    }
}

#[tonic::async_trait]
impl LogsService for Capture {
    async fn export(
        &self,
        request: Request<ExportLogsServiceRequest>,
    ) -> Result<Response<ExportLogsServiceResponse>, Status> {
        let peer = request
            .peer_certs()
            .is_some_and(|certificates| !certificates.is_empty());
        let header = request
            .metadata()
            .get("authorization")
            .unwrap()
            .to_str()
            .unwrap()
            .to_owned();
        self.received
            .try_send((peer, header, request.into_inner()))
            .map_err(|_| Status::resource_exhausted("test collector full"))?;
        let _permit = self.blocked.acquire().await.unwrap();
        Ok(Response::new(ExportLogsServiceResponse::default()))
    }
}

struct Collector {
    endpoint: String,
    received: mpsc::Receiver<(bool, String, ExportLogsServiceRequest)>,
    sdk_received: mpsc::Receiver<&'static str>,
    blocked: Arc<Semaphore>,
    task: JoinHandle<Result<(), tonic::transport::Error>>,
}

impl Collector {
    async fn start(credentials: &Credentials, mutual: bool, held: bool) -> Result<Self> {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
        let endpoint = format!("https://{}", listener.local_addr()?);
        let mut tls = ServerTlsConfig::new().identity(Identity::from_pem(
            credentials.read("server.pem")?,
            credentials.read("server.key")?,
        ));
        if mutual {
            tls = tls.client_ca_root(Certificate::from_pem(credentials.read("ca.pem")?));
        }
        let (sender, received) = mpsc::channel(8);
        let (sdk_sender, sdk_received) = mpsc::channel(8);
        let sdk = SdkCapture {
            received: sdk_sender,
            mutual,
        };
        let blocked = Arc::new(Semaphore::new(if held { 0 } else { 1 }));
        let task = tokio::spawn(
            Server::builder()
                .tls_config(tls)?
                .add_service(LogsServiceServer::new(Capture {
                    received: sender,
                    blocked: blocked.clone(),
                }))
                .add_service(MetricsServiceServer::new(sdk.clone()))
                .add_service(TraceServiceServer::new(sdk))
                .serve_with_incoming(TcpListenerStream::new(listener)),
        );
        Ok(Self {
            endpoint,
            received,
            sdk_received,
            blocked,
            task,
        })
    }
}

impl Drop for Collector {
    fn drop(&mut self) {
        self.blocked.add_permits(8);
        self.task.abort();
    }
}

fn event() -> LogEvent {
    LogEvent {
        event_name: "dst.tls.test".into(),
        body: json!({"verified":true, "private":"tls-payload-secret"}),
        observed_timestamp_ns: 1,
        severity_text: "INFO".into(),
        attributes: Default::default(),
    }
}

#[tokio::test]
async fn environment_credentials_export_all_signals_over_tls_and_mutual_tls() -> Result<()> {
    let credentials = Credentials::new()?;
    for mutual in [false, true] {
        let mut collector = Collector::start(&credentials, mutual, false).await?;
        let mut config = credentials.config(
            &collector.endpoint,
            mutual,
            &credentials.0.path().join("ca.pem"),
        )?;
        let exporter = LogExporter::new(config.clone(), &Default::default())?;
        assert_eq!(exporter.submit(&event())?, Submission::Accepted);
        let (peer, header, message) =
            tokio::time::timeout(Duration::from_secs(3), collector.received.recv())
                .await?
                .unwrap();
        assert_eq!(peer, mutual);
        assert_eq!(header, "Bearer tls-secret");
        assert_eq!(message, export_request(&Default::default(), &[event()])?);
        let stats = exporter.close().await?;
        assert_eq!(
            (stats.accepted, stats.exported, stats.pending_records),
            (1, 1, 0)
        );
        config.export_timeout = Duration::from_secs(2);
        config.schedule_delay = Duration::from_secs(60);
        let sdk = Observability::new(&TelemetryConfig {
            logs: None,
            metrics: Some(config.clone()),
            traces: Some(config),
            resource: Default::default(),
        })?;
        sdk.begin_operation("fixture", None, "tlscheck", None)
            .finish(None);
        tokio::time::timeout(Duration::from_secs(5), sdk.close()).await??;
        let mut signals = Vec::new();
        while let Ok(signal) = collector.sdk_received.try_recv() {
            signals.push(signal);
        }
        assert!(signals.contains(&"metrics") && signals.contains(&"traces"));
    }
    Ok(())
}

#[tokio::test]
async fn untrusted_ca_and_missing_client_certificate_are_accounted_without_secrets() -> Result<()> {
    let credentials = Credentials::new()?;
    let wrong = Credentials::new()?;
    for (mutual, ca) in [
        (false, wrong.0.path().join("ca.pem")),
        (true, credentials.0.path().join("ca.pem")),
    ] {
        let mut collector = Collector::start(&credentials, mutual, false).await?;
        let config = credentials.config(&collector.endpoint, false, &ca)?;
        let exporter = LogExporter::new(config, &Default::default())?;
        exporter.submit(&event())?;
        let stats = tokio::time::timeout(Duration::from_secs(3), exporter.close()).await??;
        assert_eq!(
            (
                stats.accepted,
                stats.exported,
                stats.pending_records,
                stats.pending_bytes
            ),
            (1, 0, 0, 0)
        );
        assert_eq!(stats.timed_out + stats.failed, 1);
        assert!(collector.received.try_recv().is_err());
        let public = serde_json::to_string(&stats)?;
        let private_key = String::from_utf8(credentials.read("client.key")?)?;
        for secret in [
            "tls-secret",
            "tls-payload-secret",
            private_key.lines().next().unwrap(),
            credentials.0.path().to_str().unwrap(),
            wrong.0.path().to_str().unwrap(),
        ] {
            assert!(!public.contains(secret));
        }
    }
    Ok(())
}

#[tokio::test]
async fn cancelled_tls_close_retains_the_original_drain_deadline() -> Result<()> {
    let credentials = Credentials::new()?;
    let mut collector = Collector::start(&credentials, true, true).await?;
    let mut config = credentials.config(
        &collector.endpoint,
        true,
        &credentials.0.path().join("ca.pem"),
    )?;
    config.export_timeout = Duration::from_secs(5);
    config.drain_timeout = Duration::from_millis(600);
    let exporter = Arc::new(LogExporter::new(config, &Default::default())?);
    exporter.submit(&event())?;
    tokio::time::timeout(Duration::from_secs(3), collector.received.recv())
        .await?
        .unwrap();
    exporter.submit(&event())?;
    let closing = tokio::spawn({
        let exporter = exporter.clone();
        async move { exporter.close().await }
    });
    tokio::time::timeout(Duration::from_secs(1), async {
        while !exporter.stats().closed {
            tokio::task::yield_now().await;
        }
    })
    .await?;
    tokio::time::sleep(Duration::from_millis(400)).await;
    closing.abort();
    let stats = tokio::time::timeout(Duration::from_millis(400), exporter.close()).await??;
    assert_eq!(
        (stats.accepted, stats.timed_out, stats.shutdown_dropped),
        (2, 1, 1)
    );
    assert_eq!((stats.pending_records, stats.pending_bytes), (0, 0));
    Ok(())
}
