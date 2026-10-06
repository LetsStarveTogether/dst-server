//! Observe official SDK call results without reinterpreting partial-success responses.

use crate::telemetry::health::ExporterHealth;
use opentelemetry_sdk::{
    Resource,
    error::OTelSdkResult,
    metrics::{Temporality, data::ResourceMetrics, exporter::PushMetricExporter},
    trace::{SpanData, SpanExporter},
};
use std::{sync::Arc, time::Duration};

pub(super) struct Metrics {
    pub(super) inner: opentelemetry_otlp::MetricExporter,
    pub(super) health: Arc<ExporterHealth>,
}

impl PushMetricExporter for Metrics {
    async fn export(&self, metrics: &ResourceMetrics) -> OTelSdkResult {
        let result = self.inner.export(metrics).await;
        self.health.sdk_result("metrics", "export", &result);
        result
    }

    fn force_flush(&self) -> OTelSdkResult {
        let result = self.inner.force_flush();
        self.health.sdk_result("metrics", "flush", &result);
        result
    }

    fn shutdown_with_timeout(&self, timeout: Duration) -> OTelSdkResult {
        let result = self.inner.shutdown_with_timeout(timeout);
        self.health.sdk_result("metrics", "shutdown", &result);
        result
    }

    fn temporality(&self) -> Temporality {
        self.inner.temporality()
    }
}

#[derive(Debug)]
pub(super) struct Traces {
    pub(super) inner: opentelemetry_otlp::SpanExporter,
    pub(super) health: Arc<ExporterHealth>,
}

impl SpanExporter for Traces {
    async fn export(&self, batch: Vec<SpanData>) -> OTelSdkResult {
        let result = self.inner.export(batch).await;
        self.health.sdk_result("traces", "export", &result);
        result
    }

    fn force_flush(&self) -> OTelSdkResult {
        let result = self.inner.force_flush();
        self.health.sdk_result("traces", "flush", &result);
        result
    }

    fn shutdown_with_timeout(&self, timeout: Duration) -> OTelSdkResult {
        let result = self.inner.shutdown_with_timeout(timeout);
        self.health.sdk_result("traces", "shutdown", &result);
        result
    }

    fn set_resource(&mut self, resource: &Resource) {
        self.inner.set_resource(resource);
    }
}
