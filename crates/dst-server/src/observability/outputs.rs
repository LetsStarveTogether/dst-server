//! Read existing counters when the SDK collects; no independent sampler is needed.

use super::{Shards, base_attributes};
use crate::telemetry::{
    LogExporter,
    health::{ExporterHealth, HealthKey},
    local::LocalLog,
};
use opentelemetry::{KeyValue, metrics::Meter};
use std::sync::Arc;

pub(super) type Outputs = Option<(LocalLog, Option<Arc<LogExporter>>)>;

fn labels(stage: &'static str, source: &'static str, reason: &'static str) -> [KeyValue; 3] {
    [
        KeyValue::new("dst.telemetry.stage", stage),
        KeyValue::new("dst.telemetry.source", source),
        KeyValue::new("dst.telemetry.reason", reason),
    ]
}

fn health_labels((signal, stage, reason, code, failed): HealthKey) -> Vec<KeyValue> {
    let mut labels = vec![
        KeyValue::new("dst.telemetry.signal", signal),
        KeyValue::new("dst.telemetry.stage", stage),
        KeyValue::new("dst.telemetry.reason", reason),
        KeyValue::new(
            "dst.telemetry.result",
            if failed { "failure" } else { "success" },
        ),
    ];
    if let Some(code) = code {
        labels.push(KeyValue::new("rpc.grpc.status_code", i64::from(code)));
    }
    labels
}

pub(super) fn register(
    meter: &Meter,
    outputs: Outputs,
    health: Arc<ExporterHealth>,
    shards: Shards,
) {
    let source = outputs.clone();
    meter
        .u64_observable_counter("dst.telemetry.delivery.count")
        .with_unit("{record}")
        .with_description("Records by delivery stage. A record can appear at multiple stages.")
        .with_callback(move |observer| {
            for ((cluster, shard), state) in shards.lock().unwrap().iter() {
                let [cluster, shard] = base_attributes(cluster, shard);
                let [stage, source, reason] = labels("driver_relay", "driver", "dropped");
                observer.observe(
                    state.relay_dropped,
                    &[cluster, shard, stage, source, reason],
                );
            }
            let Some((local, exporter)) = source.as_ref() else {
                return;
            };
            for (kind, stats) in local.events.stats() {
                for (reason, count) in [
                    ("accepted", stats.accepted),
                    ("invalid", stats.invalid),
                    ("too_large", stats.too_large),
                    ("byte_full", stats.byte_full),
                ] {
                    observer.observe(count, &labels("subscription", kind, reason));
                }
            }
            let local_stats = local.output.stats();
            for (reason, count) in [
                ("accepted", local_stats.accepted),
                ("written", local_stats.written),
                ("queue_full", local_stats.queue_full),
                ("byte_full", local_stats.byte_full),
                ("closed_dropped", local_stats.closed_dropped),
                ("write_failed", local_stats.write_failed),
            ] {
                observer.observe(count, &labels("local_output", "logs", reason));
            }
            if let Some(exporter) = exporter {
                let stats = exporter.stats();
                for (reason, count) in [
                    ("accepted", stats.accepted),
                    ("invalid", stats.invalid),
                    ("too_large", stats.too_large),
                    ("queue_full", stats.queue_full),
                    ("byte_full", stats.queue_bytes_full),
                    ("closed", stats.closed_dropped),
                ] {
                    observer.observe(count, &labels("otlp_admission", "logs", reason));
                }
                for (reason, count) in [
                    ("acknowledged", stats.exported),
                    ("rejected", stats.rejected),
                    ("failed", stats.failed),
                    ("confirmation_timeout", stats.timed_out),
                    ("shutdown_dropped", stats.shutdown_dropped),
                ] {
                    observer.observe(count, &labels("export", "logs", reason));
                }
            }
        })
        .build();
    for bytes in [false, true] {
        let source = outputs.clone();
        meter
            .u64_observable_gauge(if bytes {
                "dst.telemetry.pending.bytes"
            } else {
                "dst.telemetry.pending.records"
            })
            .with_unit(if bytes { "By" } else { "{record}" })
            .with_callback(move |observer| {
                let Some((local, exporter)) = source.as_ref() else {
                    return;
                };
                for (kind, stats) in local.events.stats() {
                    observer.observe(
                        if bytes {
                            stats.pending_bytes
                        } else {
                            stats.pending_records
                        } as u64,
                        &labels("subscription", kind, "pending"),
                    );
                }
                let local_stats = local.output.stats();
                observer.observe(
                    if bytes {
                        local_stats.pending_bytes
                    } else {
                        local_stats.pending_records
                    } as u64,
                    &labels("local_output", "logs", "pending"),
                );
                if let Some(exporter) = exporter {
                    let stats = exporter.stats();
                    observer.observe(
                        if bytes {
                            stats.pending_bytes
                        } else {
                            stats.pending_records
                        } as u64,
                        &labels("otlp", "logs", "pending"),
                    );
                }
            })
            .build();
    }
    for (name, unit, transitions) in [
        ("dst.telemetry.exporter.call.count", "{call}", false),
        (
            "dst.telemetry.exporter.transition.count",
            "{transition}",
            true,
        ),
    ] {
        let source = outputs.clone();
        let health = health.clone();
        meter
            .u64_observable_counter(name)
            .with_unit(unit)
            .with_callback(move |observer| {
                let (calls, changes) = health.counters();
                for (key, count) in if transitions { changes } else { calls } {
                    observer.observe(count, &health_labels(key));
                }
                if let Some((_, Some(exporter))) = source.as_ref() {
                    let (calls, changes) = exporter.health_counters();
                    for (key, count) in if transitions { changes } else { calls } {
                        observer.observe(count, &health_labels(key));
                    }
                }
            })
            .build();
    }
}
