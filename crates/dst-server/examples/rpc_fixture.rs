//! Local transport fixture for Python cancellation and interpreter lifecycle checks.
use dst_server::{
    model::Request,
    rpc::{self, Incoming},
};
use serde_json::json;
use std::{
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
};
use tokio::sync::{Notify, mpsc, oneshot};

#[tokio::main(flavor = "current_thread")]
async fn main() -> anyhow::Result<()> {
    let socket = PathBuf::from(std::env::args_os().nth(1).expect("socket path"));
    tokio::task::LocalSet::new()
        .run_until(async move {
            let (dispatch, mut incoming) = mpsc::channel::<Incoming>(64);
            let (shutdown, stop) = oneshot::channel();
            let accepted = Arc::new(AtomicU64::new(0));
            let completed = Arc::new(AtomicU64::new(0));
            let release = Arc::new(Notify::new());
            tokio::spawn(async move {
                while let Some(call) = incoming.recv().await {
                    let accepted = accepted.clone();
                    let completed = completed.clone();
                    let release = release.clone();
                    tokio::spawn(async move {
                        if matches!(call.request.request, Request::Save {}) {
                            accepted.fetch_add(1, Ordering::SeqCst);
                            release.notified().await;
                            completed.fetch_add(1, Ordering::SeqCst);
                        } else if matches!(call.request.request, Request::Start {}) {
                            // The test releases accepted saves after disconnecting their caller.
                            release.notify_one();
                        }
                        let _ = call.reply.send(Ok(json!({
                            "accepted": accepted.load(Ordering::SeqCst),
                            "completed": completed.load(Ordering::SeqCst),
                            "exact": 9_007_199_254_740_993_u64,
                            "nested": [null, false, 0, "", {"float": 0.25}],
                        })));
                    });
                }
            });
            tokio::spawn(async move {
                let _ = tokio::signal::ctrl_c().await;
                let _ = shutdown.send(());
            });
            rpc::serve(&socket, dispatch, rpc::EventHub::default(), stop).await
        })
        .await?;
    Ok(())
}
