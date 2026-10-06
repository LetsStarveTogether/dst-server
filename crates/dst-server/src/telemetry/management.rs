//! Bounded summaries of the current recovery facts, emitted only after persistence.

use crate::recovery::{Decision, RecoveryState, RunState};
use serde_json::{Value, json};
use std::collections::BTreeMap;

pub(crate) fn lua_diagnostic(
    diagnostic: Option<&crate::process::NativeDiagnostic>,
    observed: &mut u64,
    cluster: &str,
    shard: &str,
    process: &str,
    nonce: &str,
    generation: Option<u64>,
) -> Option<super::LogEvent> {
    let diagnostic = diagnostic?;
    let count = diagnostic.count.saturating_sub(*observed);
    if count == 0 {
        return None;
    }
    *observed = diagnostic.count;
    let mut log = super::LogEvent::new(
        "dst.runtime.diagnostic",
        json!({"kind":"lua_error","count":count,"total":diagnostic.count,"observed_at_ms":diagnostic.observed_at_ms,"process_id":process}),
        "ERROR",
    );
    log.attributes.extend([
        ("dst.cluster.name".into(), json!(cluster)),
        ("dst.shard.name".into(), json!(shard)),
        ("dst.game.attempt.id".into(), json!(nonce)),
        ("dst.runtime.generation".into(), json!(generation)),
    ]);
    Some(log)
}

pub(crate) fn recovery_records(state: &RecoveryState, run: &RunState) -> BTreeMap<String, Value> {
    let failure = state.last_failure.as_ref();
    let decision = failure
        .and_then(|failure| failure.decision.as_ref())
        .map(|decision| match decision {
            Decision::RetryCurrent {
                attempt,
                not_before_ms,
                ..
            } => json!({"action":"retry_current","attempt":attempt,"not_before_ms":not_before_ms}),
            Decision::Restore { target, resumed } => {
                json!({"action":"restore","snapshot_id":target.snapshot_id,"resumed":resumed})
            }
            Decision::StayClosed { reason } => json!({"action":"stay_closed","reason":reason}),
        });
    let mut records = BTreeMap::from([(
        String::new(),
        json!({
            "attempt_id":failure.map(|failure| &failure.attempt_id),
            "operation_id":failure.and_then(|failure| failure.trigger.operation_id.as_ref()),
            "recovery_attempt_id":failure.and_then(|failure| failure.recovery_attempt_id.as_ref()),
            "run_attempt_id":run.attempt_id,"active":run.active,
            "cause":failure.map(|failure| failure.cause),
            "trigger":failure.map(|failure| &failure.trigger),
            "complete":failure.map(|failure| failure.complete),
            "classification":failure.map(|failure| json!(failure.classification)["kind"].clone()),
            "catalog_error":failure.and_then(|failure| failure.catalog_error.as_ref()),
            "decision":decision,"result":failure.and_then(|failure| failure.result),
            "restarts_used":state.restarts_used,"closed":state.closed,
            "retry_started":state.retry.as_ref().map(|retry| retry.started),
            "target_phase":state.target.as_ref().map(|target| target.phase),
        }),
    )]);
    if let Some(failure) = failure {
        for (name, shard) in &failure.shards {
            records.insert(name.clone(), json!({
                "attempt_id":failure.attempt_id,"recovery_attempt_id":failure.recovery_attempt_id,
                "shard":name,"process_id":shard.process_id,"nonce":shard.nonce,"generation":shard.generation,
                "failed":shard.failed,"cause":shard.cause,"evidence_current":shard.evidence_current,
                "native_diagnostics":shard.native_diagnostics,"control_error":shard.control_error,
                "exit":shard.exit.as_ref().map(|exit| json!({"returncode":exit.returncode,"forced":exit.forced,"output_drained":exit.output_drained,"protocol_error":exit.protocol_error.is_some(),"read_errors":exit.stats.read_errors})),
            }));
        }
    }
    records
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn final_exit_severity_uses_final_facts_and_diagnostics_are_counted_once() {
        let event = crate::driver::DriverEvent {
            id: ulid::Ulid::generate().to_string(),
            shard: "Master".into(),
            nonce: ulid::Ulid::generate().to_string(),
            generation: Some(2),
            observed_timestamp_ns: 1,
            kind: crate::driver::DriverEventKind::Lifecycle(
                json!({"event":"process_exited","exit":{"returncode":0,"forced":false,"output_drained":true,"protocol_error":null},"control_error":null,"native_diagnostics":{"lua_error":{"count":5}}}),
            ),
        };
        assert_eq!(
            super::super::LogEvent::from_driver(event.clone(), "room")
                .unwrap()
                .severity_text,
            "INFO"
        );
        for (pointer, value, severity) in [
            ("/exit/forced", json!(true), "WARN"),
            ("/exit/returncode", json!(1), "ERROR"),
            ("/exit/output_drained", json!(false), "ERROR"),
            ("/exit/protocol_error", json!("invalid frame"), "ERROR"),
            ("/control_error", json!({"code":"protocol"}), "ERROR"),
        ] {
            let mut event = event.clone();
            let crate::driver::DriverEventKind::Lifecycle(data) = &mut event.kind else {
                unreachable!()
            };
            *data.pointer_mut(pointer).unwrap() = value;
            assert_eq!(
                super::super::LogEvent::from_driver(event, "room")
                    .unwrap()
                    .severity_text,
                severity
            );
        }
        let mut observed = 0;
        for (count, expected) in [(5, Some(5)), (5, None), (4, None), (8, Some(3))] {
            let diagnostic = crate::process::NativeDiagnostic {
                kind: crate::process::NativeDiagnosticKind::LuaError,
                count,
                observed_at_ms: 100,
            };
            let log = lua_diagnostic(
                Some(&diagnostic),
                &mut observed,
                "room",
                "Master",
                "process",
                "nonce",
                Some(2),
            );
            assert_eq!(
                log.as_ref().map(|log| log.body["count"].as_u64().unwrap()),
                expected
            );
            if let Some(log) = log {
                assert_eq!(log.severity_text, "ERROR");
            }
        }
    }
}
