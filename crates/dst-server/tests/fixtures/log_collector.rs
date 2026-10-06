use opentelemetry_proto::tonic::collector::logs::v1::{
    ExportLogsServiceRequest, ExportLogsServiceResponse,
    logs_service_server::{LogsService, LogsServiceServer},
};
use std::{
    collections::VecDeque,
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{
    sync::{Semaphore, mpsc, oneshot},
    task::JoinHandle,
    time::Instant,
};
use tokio_stream::wrappers::TcpListenerStream;
use tonic::{
    Request, Response, Status, codec::CompressionEncoding, metadata::MetadataMap, transport::Server,
};

pub(crate) struct CapturedRequest {
    pub(crate) message: ExportLogsServiceRequest,
    pub(crate) metadata: MetadataMap,
    pub(crate) at: Instant,
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

pub(crate) struct Collector {
    pub(crate) endpoint: String,
    pub(crate) received: mpsc::Receiver<CapturedRequest>,
    shutdown: oneshot::Sender<()>,
    task: JoinHandle<Result<(), tonic::transport::Error>>,
    blocked: Option<Arc<Semaphore>>,
}

impl Collector {
    pub(crate) async fn start(
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

    pub(crate) async fn receive(&mut self) -> anyhow::Result<CapturedRequest> {
        Ok(
            tokio::time::timeout(Duration::from_secs(2), self.received.recv())
                .await?
                .unwrap(),
        )
    }

    pub(crate) async fn stop(self) -> anyhow::Result<()> {
        if let Some(blocked) = self.blocked {
            blocked.add_permits(1);
        }
        let _ = self.shutdown.send(());
        tokio::time::timeout(Duration::from_secs(2), self.task).await???;
        Ok(())
    }
}
