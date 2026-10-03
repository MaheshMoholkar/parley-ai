"""An in-memory span collector for tests.

OpenTelemetry allows one tracer provider per process, so it is installed once
and each test clears what earlier tests recorded.
"""

from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

_exporter: InMemorySpanExporter | None = None


def collected_spans() -> InMemorySpanExporter:
    """Install the collector (once) and return it, emptied."""
    global _exporter
    if _exporter is None:
        _exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(_exporter))
        trace.set_tracer_provider(provider)
    _exporter.clear()
    return _exporter


def named(spans: tuple[ReadableSpan, ...], name: str) -> list[ReadableSpan]:
    return [span for span in spans if span.name == name]
