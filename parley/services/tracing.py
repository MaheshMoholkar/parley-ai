"""Tracing (spec: "OpenTelemetry to CloudWatch, one trace per case step").

Each unit of work the worker does is wrapped in `step(...)`: syncing a tenant,
acting on one customer's due cases, drafting one message, delivering one,
reading one reply, one investigation, one call, posting one webhook. In the
worker nothing encloses these, so each becomes its own trace; when an API
request does the work (POST /v1/sync), it joins that request's trace instead.
Model calls are child spans with their token counts and cost.

This module only uses the OpenTelemetry API. Until `bootstrap.configure_tracing`
installs an exporter, every span is a no-op, so tests and local runs need
nothing.

Attribute names are prefixed "parley." (parley.tenant_id, parley.case_ids...),
so a trace can be found from any id the API or the review screen shows.
"""

import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Span

tracer = trace.get_tracer("parley")

AttributeValue = str | int | float | bool | uuid.UUID | Sequence[Any] | None


@contextmanager
def step(name: str, **attributes: AttributeValue) -> Iterator[Span]:
    """A span for one step. An exception escaping the block marks the span as
    an error (and is re-raised)."""
    with tracer.start_as_current_span(name, attributes=_clean(attributes)) as span:
        yield span


def annotate(span: Span, **attributes: AttributeValue) -> None:
    """Add what was learnt during the step, such as its outcome."""
    span.set_attributes(_clean(attributes))


def _clean(attributes: dict[str, AttributeValue]) -> dict[str, Any]:
    """OpenTelemetry takes strings, numbers, booleans and lists of those. Ids
    become strings and missing values are left out."""
    clean: dict[str, Any] = {}
    for key, value in attributes.items():
        if value is None:
            continue
        if isinstance(value, uuid.UUID):
            value = str(value)
        elif isinstance(value, Sequence) and not isinstance(value, str):
            value = [str(v) if isinstance(v, uuid.UUID) else v for v in value]
        elif not isinstance(value, str | int | float | bool):
            value = str(value)
        clean[f"parley.{key}"] = value
    return clean
