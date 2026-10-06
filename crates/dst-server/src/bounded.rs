//! Nonblocking single-consumer channels with a payload byte budget.

use std::sync::Arc;
use tokio::sync::{OwnedSemaphorePermit, Semaphore, mpsc};

pub(crate) fn channel<T>(slots: usize, bytes: usize) -> (Sender<T>, mpsc::Receiver<Item<T>>) {
    let (queue, receiver) = mpsc::channel(slots);
    let bytes = Arc::new(Semaphore::new(bytes));
    (Sender { queue, bytes }, receiver)
}

pub(crate) struct Sender<T> {
    queue: mpsc::Sender<Item<T>>,
    bytes: Arc<Semaphore>,
}

impl<T> Clone for Sender<T> {
    fn clone(&self) -> Self {
        Self {
            queue: self.queue.clone(),
            bytes: self.bytes.clone(),
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum TrySendError {
    Slots,
    Bytes,
    Closed,
}

impl<T> Sender<T> {
    pub(crate) fn try_send(&self, value: T, bytes: usize) -> Result<(), TrySendError> {
        let slot = self.queue.try_reserve().map_err(|error| match error {
            mpsc::error::TrySendError::Full(()) => TrySendError::Slots,
            mpsc::error::TrySendError::Closed(()) => TrySendError::Closed,
        })?;
        let bytes = u32::try_from(bytes).map_err(|_| TrySendError::Bytes)?;
        let permit = self
            .bytes
            .clone()
            .try_acquire_many_owned(bytes)
            .map_err(|_| TrySendError::Bytes)?;
        slot.send(Item {
            value,
            _bytes: permit,
        });
        Ok(())
    }

    pub(crate) fn is_closed(&self) -> bool {
        self.queue.is_closed()
    }
}

pub(crate) struct Item<T> {
    value: T,
    _bytes: OwnedSemaphorePermit,
}

impl<T> Item<T> {
    pub(crate) fn value(&self) -> &T {
        &self.value
    }

    pub(crate) fn into_inner(self) -> T {
        self.value
    }
}
