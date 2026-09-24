"""Optional OpenTelemetry tracing: one attempt is a trace, its steps are spans.

Tracing stays off unless ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, and a collector that is
unreachable never fails a run: spans are batched, exported best-effort and dropped on error.
The trace context the control plane puts on the queue message becomes the parent context
for Worker spans. The control plane currently generates that context without an exported
API producer span, so these spans do not prove end-to-end API or broker timing.
"""

import logging
import os
from contextlib import contextmanager, nullcontext

_provider = None
_unavailable = False


def provider():
    """The process-wide provider, created on first use and shared by every attempt.

    A misconfigured or unreachable collector disables tracing instead of failing runs.
    """
    global _provider, _unavailable
    if _provider is None and not _unavailable and os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            _provider = TracerProvider(resource=Resource.create(
                {"service.name": os.getenv("OTEL_SERVICE_NAME", "repopilot-worker")}))
            endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").rstrip("/")
            # The endpoint is configured as a base URL; the signal path is part of the HTTP protocol.
            traces = endpoint if endpoint.endswith("/v1/traces") else endpoint + "/v1/traces"
            _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=traces, timeout=5)))
        except Exception as error:
            _unavailable = True
            _provider = None
            logging.warning("Tracing disabled: %s", error)
    return _provider


class Tracing:
    """A no-op unless tracing is configured, so callers never branch on it."""

    def __init__(self, run_id, traceparent=None):
        self.run_id = run_id
        self.provider = provider()
        self.tracer = self.provider.get_tracer("repopilot") if self.provider else None
        self.parent = self._extract(traceparent)
        # Read the handed-over span now: opening the attempt span consumes the parent context.
        self._parent_span_id = self._span_id_of(self.parent)
        self._trace_id = None

    @staticmethod
    def _span_id_of(context):
        if not context:
            return None
        from opentelemetry import trace

        span_context = trace.get_current_span(context).get_span_context()
        return f"{span_context.span_id:016x}" if span_context.span_id else None

    def _extract(self, traceparent):
        if not self.tracer or not traceparent:
            return None
        try:
            from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

            return TraceContextTextMapPropagator().extract({"traceparent": traceparent})
        except Exception as error:  # a malformed header must not fail the attempt
            logging.warning("Ignoring unusable trace context: %s", error)
            return None

    @property
    def enabled(self):
        return self.tracer is not None

    @contextmanager
    def span(self, name, **attributes):
        if not self.tracer:
            yield None
            return
        from opentelemetry.trace import SpanKind

        with self.tracer.start_as_current_span(
                name, context=self.parent, kind=SpanKind.INTERNAL,
                attributes={key: value for key, value in attributes.items() if value is not None}) as span:
            # Only the attempt span continues the handed-over context; the steps are its children.
            self.parent = None
            self._trace_id = f"{span.get_span_context().trace_id:032x}"
            yield span

    def completed(self, name, started, ended, **attributes):
        """A span for a step that was measured outside a context manager, in seconds."""
        if not self.tracer:
            return
        span = self.tracer.start_span(
            name, start_time=int(started * 1e9),
            attributes={key: value for key, value in attributes.items() if value is not None})
        self._trace_id = self._trace_id or f"{span.get_span_context().trace_id:032x}"
        span.end(end_time=int(ended * 1e9))

    def trace_id(self):
        """The trace this attempt belongs to, for the run record and the workbench link."""
        if not self._trace_id and self.tracer:
            from opentelemetry import trace

            context = trace.get_current_span().get_span_context()
            self._trace_id = f"{context.trace_id:032x}" if context.trace_id else None
        return self._trace_id

    def parent_span_id(self):
        """The span the control plane handed over, so the continuation can be checked."""
        return self._parent_span_id

    def close(self):
        if self.provider:
            self.provider.force_flush(timeout_millis=5000)


def span(tracing, name, **attributes):
    """Trace a block when a tracing instance is configured, otherwise do nothing."""
    return tracing.span(name, **attributes) if tracing else nullcontext()
