import os
import logging
from opentelemetry import trace, metrics, propagate
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader, ConsoleMetricExporter
from opentelemetry.sdk.resources import Resource, SERVICE_NAME

_log = logging.getLogger(__name__)


def setup(service_name: str):
    """Initialize tracer and meter for a service. Call once at module startup."""
    resource = Resource({SERVICE_NAME: service_name})
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")

    # traces
    tp = TracerProvider(resource=resource)
    if endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        tp.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
        )
        _log.info(f"[otel] traces → {endpoint}")
    else:
        tp.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        _log.info("[otel] traces → console (set OTEL_EXPORTER_OTLP_ENDPOINT for remote)")
    trace.set_tracer_provider(tp)

    # metrics
    if endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=endpoint, insecure=True)
        )
    else:
        reader = PeriodicExportingMetricReader(
            ConsoleMetricExporter(), export_interval_millis=60_000
        )
    metrics.set_meter_provider(MeterProvider(resource=resource, metric_readers=[reader]))

    return trace.get_tracer(service_name), metrics.get_meter(service_name)


def grpc_inject() -> list:
    """Capture current span context and return as gRPC metadata pairs."""
    carrier: dict = {}
    propagate.inject(carrier)
    return list(carrier.items())


def grpc_extract(grpc_context):
    """Extract OTel context from incoming gRPC invocation metadata."""
    carrier = {m.key: m.value for m in grpc_context.invocation_metadata()}
    return propagate.extract(carrier)
