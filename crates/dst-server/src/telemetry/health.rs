//! Export observations stay local and cannot recursively enter an exporter.

use super::{LogEvent, local::LocalLog};
use crate::rpc::StreamKind;
use serde_json::json;
use std::{collections::BTreeMap, sync::Mutex};

#[cfg(test)]
#[path = "../../tests/fixtures/log_collector.rs"]
mod log_collector;

pub(crate) type HealthKey = (&'static str, &'static str, &'static str, Option<i32>, bool);

#[derive(Clone)]
struct Failure {
    reason: &'static str,
    code: Option<i32>,
    count: u64,
    records: u64,
    first_at_ns: u64,
}

#[derive(Default)]
struct State {
    failures: BTreeMap<(&'static str, &'static str), Failure>,
    calls: BTreeMap<HealthKey, u64>,
    transitions: BTreeMap<HealthKey, u64>,
}

pub(crate) struct ExporterHealth {
    local: Option<LocalLog>,
    state: Mutex<State>,
}

impl std::fmt::Debug for ExporterHealth {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("ExporterHealth")
            .finish_non_exhaustive()
    }
}

impl ExporterHealth {
    pub(crate) fn new(local: Option<LocalLog>) -> Self {
        Self {
            local,
            state: Mutex::default(),
        }
    }

    pub(crate) fn counters(&self) -> (BTreeMap<HealthKey, u64>, BTreeMap<HealthKey, u64>) {
        let state = self.state.lock().unwrap();
        (state.calls.clone(), state.transitions.clone())
    }

    pub(crate) fn observe(
        &self,
        signal: &'static str,
        stage: &'static str,
        reason: &'static str,
        code: Option<i32>,
        failed: bool,
        records: u64,
    ) {
        let key = (signal, stage, reason, code, failed);
        let mut state = self.state.lock().unwrap();
        let body = {
            *state.calls.entry(key).or_default() += 1;
            if failed {
                let current = state.failures.get_mut(&(signal, stage));
                if let Some(current) =
                    current.filter(|current| current.reason == reason && current.code == code)
                {
                    current.count += 1;
                    current.records += records;
                    return;
                }
                let now = crate::operation::timestamp();
                let previous = state.failures.insert(
                    (signal, stage),
                    Failure {
                        reason,
                        code,
                        count: 1,
                        records,
                        first_at_ns: now,
                    },
                );
                *state.transitions.entry(key).or_default() += 1;
                json!({"signal":signal,"stage":stage,"reason":reason,"code":code,"result":"failure","count":1,"records":records,"first_at_ns":now,"previous_count":previous.as_ref().map(|previous| previous.count),"previous_code":previous.as_ref().and_then(|previous| previous.code),"previous_records":previous.as_ref().map(|previous| previous.records),"previous_first_at_ns":previous.as_ref().map(|previous| previous.first_at_ns),"previous_reason":previous.map(|previous| previous.reason)})
            } else if let Some(previous) = state.failures.remove(&(signal, stage)) {
                *state.transitions.entry(key).or_default() += 1;
                json!({"signal":signal,"stage":stage,"reason":reason,"code":code,"result":"recovered","count":previous.count,"records":previous.records,"first_at_ns":previous.first_at_ns,"previous_reason":previous.reason,"previous_code":previous.code})
            } else {
                return;
            }
        };
        if let Some(local) = &self.local {
            local.publish(
                &LogEvent::new(
                    "dst.telemetry.exporter_health",
                    body,
                    if failed { "WARN" } else { "INFO" },
                ),
                StreamKind::Lifecycle,
            );
        }
        drop(state);
    }

    pub(crate) fn sdk_result(
        &self,
        signal: &'static str,
        stage: &'static str,
        result: &opentelemetry_sdk::error::OTelSdkResult,
    ) {
        use opentelemetry_sdk::error::OTelSdkError;
        let reason = match result {
            Ok(()) if stage == "export" => "export_call_success",
            Ok(()) => "call_success",
            Err(OTelSdkError::AlreadyShutdown) => "closed",
            Err(OTelSdkError::Timeout(_)) => "timeout",
            Err(OTelSdkError::InternalFailure(_)) => "internal_failure",
        };
        self.observe(signal, stage, reason, None, result.is_err(), 0);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{rpc::EventHub, telemetry::local::LocalOutput};
    use std::sync::Arc;

    #[tokio::test]
    async fn collector_results_drive_local_health_and_shutdown_accounts_for_pending_data() {
        use crate::telemetry::{ExportConfig, LogExporter, Submission};
        use opentelemetry_proto::tonic::collector::logs::v1::{
            ExportLogsPartialSuccess, ExportLogsServiceResponse,
        };
        use std::time::Duration;
        use tonic::{Code, Status};
        use tonic_types::{ErrorDetails, StatusExt};
        let hub = EventHub::default();
        let mut records = hub.subscribe_for_test(StreamKind::Lifecycle);
        let output = LocalOutput::new();
        let local = LocalLog {
            events: hub,
            resource: Arc::new(Default::default()),
            output: output.clone(),
        };
        let mut collector = log_collector::Collector::start(
            vec![
                Err(Status::unavailable("PRIVATE_ERROR")),
                Err(Status::unavailable("PRIVATE_ERROR")),
                Err(Status::with_error_details(
                    Code::ResourceExhausted,
                    "PRIVATE_ERROR",
                    ErrorDetails::with_retry_info(Some(Duration::from_millis(20))),
                )),
                Ok(ExportLogsServiceResponse {
                    partial_success: Some(ExportLogsPartialSuccess {
                        rejected_log_records: 1,
                        error_message: "PRIVATE_ERROR".into(),
                    }),
                }),
                Err(Status::permission_denied("PRIVATE_ERROR")),
            ],
            None,
        )
        .await
        .unwrap();
        let exporter = LogExporter::with_local(
            ExportConfig {
                endpoint: collector.endpoint.clone(),
                schedule_delay: Duration::from_secs(60),
                ..Default::default()
            },
            &Default::default(),
            Some(local.clone()),
        )
        .unwrap();
        let log = LogEvent::new("dst.test", json!({"null":null}), "INFO");
        exporter.submit(&log).unwrap();
        exporter.submit(&log).unwrap();
        let stats = exporter.flush().await.unwrap();
        assert_eq!((stats.exported, stats.rejected, stats.retries), (1, 1, 3));
        let first = collector.receive().await.unwrap();
        let mut prior = first.at;
        for index in 0..3 {
            let retry = collector.receive().await.unwrap();
            assert_eq!(retry.message, first.message);
            assert_eq!(retry.metadata.get("authorization"), None);
            if index == 2 {
                assert!(retry.at.duration_since(prior) >= Duration::from_millis(20));
            }
            prior = retry.at;
        }
        exporter.submit(&log).unwrap();
        assert_eq!(exporter.flush().await.unwrap().failed, 1);
        exporter.submit(&log).unwrap();
        assert_eq!(exporter.flush().await.unwrap().exported, 2);
        exporter.close().await.unwrap();
        let observations = records.drain();
        let reasons: Vec<_> = observations
            .iter()
            .map(|record| record["body"]["reason"].as_str().unwrap())
            .collect();
        assert_eq!(
            reasons,
            [
                "retry",
                "retry",
                "partial_rejection",
                "permanent_error",
                "acknowledged"
            ]
        );
        assert_eq!(observations[1]["body"]["previous_count"], 2);
        assert_eq!(observations[4]["body"]["result"], "recovered");
        assert!(!json!(observations).to_string().contains("PRIVATE_ERROR"));
        collector.stop().await.unwrap();

        let blocked = Arc::new(tokio::sync::Semaphore::new(0));
        let mut collector = log_collector::Collector::start(vec![], Some(blocked))
            .await
            .unwrap();
        let exporter = LogExporter::with_local(
            ExportConfig {
                endpoint: collector.endpoint.clone(),
                queue_capacity: 2,
                max_batch_records: 1,
                drain_timeout: Duration::from_millis(30),
                ..Default::default()
            },
            &Default::default(),
            Some(local),
        )
        .unwrap();
        exporter.submit(&log).unwrap();
        collector.receive().await.unwrap();
        exporter.submit(&log).unwrap();
        assert_eq!(exporter.submit(&log).unwrap(), Submission::QueueFull);
        let stats = exporter.close().await.unwrap();
        assert_eq!((stats.timed_out, stats.shutdown_dropped), (1, 1));
        assert_eq!(exporter.submit(&log).unwrap(), Submission::Closed);
        let observations = records.drain();
        assert!(
            observations
                .iter()
                .any(|record| record["body"]["reason"] == "confirmation_timeout")
        );
        assert!(
            observations
                .iter()
                .any(|record| record["body"]["stage"] == "shutdown"
                    && record["body"]["records"] == 1)
        );
        assert!(
            observations
                .iter()
                .any(|record| record["body"]["stage"] == "admission"
                    && record["body"]["reason"] == "closed"
                    && record["body"]["previous_reason"] == "queue_full")
        );
        collector.stop().await.unwrap();
        assert!(output.close().await);
    }

    #[tokio::test]
    async fn failures_merge_by_stage_and_preserve_categories_before_recovery() {
        let hub = EventHub::default();
        let mut records = hub.subscribe_for_test(StreamKind::Lifecycle);
        let output = LocalOutput::new();
        let health = ExporterHealth::new(Some(LocalLog {
            events: hub,
            resource: Arc::new(Default::default()),
            output: output.clone(),
        }));
        health.observe("logs", "admission", "queue_full", None, true, 1);
        for _ in 0..3 {
            health.observe("logs", "export", "retry", Some(14), true, 2);
        }
        health.observe("logs", "export", "permanent_error", Some(7), true, 2);
        health.observe("logs", "export", "acknowledged", Some(0), false, 2);
        health.observe("logs", "export", "acknowledged", Some(0), false, 2);
        assert!(
            health
                .state
                .lock()
                .unwrap()
                .failures
                .contains_key(&("logs", "admission"))
        );
        health.observe("logs", "admission", "accepted", None, false, 1);
        let records = records.drain();
        assert_eq!(records.len(), 5);
        assert_eq!(records[2]["body"]["previous_count"], 3);
        assert_eq!(records[2]["body"]["previous_records"], 6);
        assert_eq!(records[2]["body"]["previous_code"], 14);
        assert_eq!(
            records[2]["body"]["previous_first_at_ns"],
            records[1]["body"]["first_at_ns"]
        );
        assert_eq!(records[3]["body"]["previous_reason"], "permanent_error");
        assert_eq!(records[3]["body"]["result"], "recovered");
        assert_eq!(records[4]["body"]["stage"], "admission");
        assert!(health.state.lock().unwrap().failures.is_empty());
        assert!(output.close().await);
    }
}
