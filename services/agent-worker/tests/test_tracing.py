import pytest
from repopilot import tracing


def test_tracing_is_a_no_op_without_a_collector(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr(tracing, "_provider", None)
    monkeypatch.setattr(tracing, "_unavailable", True)
    instance = tracing.Tracing("run-1", "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
    assert instance.enabled is False
    assert instance.trace_id() is None and instance.parent_span_id() is None
    with tracing.span(instance, "attempt", run_id="run-1") as span:
        assert span is None
    instance.completed("model.call", 0.0, 1.0, call=1)
    instance.close()


def test_callers_can_span_a_block_without_tracing():
    with tracing.span(None, "attempt") as span:
        assert span is None


def test_the_handed_over_trace_context_is_continued(monkeypatch):
    pytest.importorskip("opentelemetry.sdk.trace")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setattr(tracing, "_provider", None)
    monkeypatch.setattr(tracing, "_unavailable", False)
    try:
        instance = tracing.Tracing("run-1", "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
        assert instance.enabled is True
        assert instance.parent_span_id() == "00f067aa0ba902b7"
        with tracing.span(instance, "attempt", run_id="run-1") as span:
            assert span is not None
            assert instance.trace_id() == "4bf92f3577b34da6a3ce929d0e0e4736"
    finally:
        tracing._provider = None
        tracing._unavailable = False


def test_a_malformed_trace_context_is_ignored_not_fatal(monkeypatch):
    pytest.importorskip("opentelemetry.sdk.trace")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setattr(tracing, "_provider", None)
    monkeypatch.setattr(tracing, "_unavailable", False)
    try:
        instance = tracing.Tracing("run-1", "not-a-traceparent")
        assert instance.enabled is True
        assert instance.parent_span_id() is None  # the header is dropped, the attempt still runs
        assert tracing.provider() is not None
    finally:
        tracing._provider = None
        tracing._unavailable = False
