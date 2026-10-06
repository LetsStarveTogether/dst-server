use futures::{StreamExt, stream::FuturesUnordered};

/// Refill concurrency slots as work finishes, then return results in input order.
pub(crate) async fn collect<F: Future>(
    tasks: impl Iterator<Item = F>,
    concurrency: usize,
) -> Vec<F::Output> {
    assert!(concurrency > 0);
    let mut tasks = tasks.enumerate().fuse();
    let mut active = FuturesUnordered::new();
    let mut results = Vec::new();
    loop {
        for (index, task) in tasks.by_ref().take(concurrency - active.len()) {
            active.push(async move { (index, task.await) });
        }
        let Some(result) = active.next().await else {
            break;
        };
        results.push(result);
    }
    results.sort_unstable_by_key(|(index, _)| *index);
    results.into_iter().map(|(_, result)| result).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;
    use tokio::sync::{mpsc, oneshot};

    #[tokio::test]
    async fn slow_first_task_keeps_slots_available_and_results_ordered() {
        tokio::time::timeout(Duration::from_secs(3), async {
            let (started, mut starts) = mpsc::unbounded_channel();
            let (mut releases, waits): (Vec<_>, Vec<_>) = (0..3)
                .map(|_| {
                    let (send, receive) = oneshot::channel();
                    (Some(send), receive)
                })
                .unzip();
            let tasks = waits.into_iter().enumerate().map(move |(index, wait)| {
                let started = started.clone();
                async move {
                    started.send(index).unwrap();
                    wait.await.unwrap();
                    index
                }
            });
            let task = tokio::spawn(collect(tasks, 2));
            let mut admitted = [starts.recv().await.unwrap(), starts.recv().await.unwrap()];
            admitted.sort_unstable();
            assert_eq!(admitted, [0, 1]);
            assert!(starts.try_recv().is_err());
            releases[1].take().unwrap().send(()).unwrap();
            assert_eq!(starts.recv().await.unwrap(), 2);
            releases[2].take().unwrap().send(()).unwrap();
            assert!(!task.is_finished());
            releases[0].take().unwrap().send(()).unwrap();
            assert_eq!(task.await.unwrap(), [0, 1, 2]);
        })
        .await
        .unwrap();
    }
}
