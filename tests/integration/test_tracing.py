"""Tracing: one trace per case step, model calls inside them, ids on every
span, and the trace id on log lines."""

import json
import logging
from datetime import timedelta
from pathlib import Path

import pytest

from parley.adapters.models.fake import FakeCall, FakeModel
from parley.bootstrap import _TraceIdFilter
from parley.collections_ai.jobs import DraftOut, ToneVerdict
from parley.services.runtime import Runtime
from parley.services.tracing import step
from parley.services.worker import run_once
from tests.integration.conftest import invoice_row, make_tenant, write_aging
from tests.tracing import collected_spans, named


def drafter(call: FakeCall) -> DraftOut | ToneVerdict:
    if call.output_type is ToneVerdict:
        return ToneVerdict(passed=True, reason="polite")
    facts = json.loads(call.prompt)
    invoice = facts["invoices"][0]
    return DraftOut(
        subject=f"Invoice {invoice['number']}",
        body=f"Invoice {invoice['number']} for {invoice['amount_due']} was due on "
        f"{invoice['due_date']}.",
    )


def test_each_case_step_is_its_own_trace(rt: Runtime, tmp_path: Path) -> None:
    exporter = collected_spans()
    rt.model = FakeModel(drafter)
    aging = write_aging(tmp_path / "aging.csv", [invoice_row("A-1", "Asha", "1000", "2026-01-01")])
    tenant_id = make_tenant(rt, aging, approval_mode="none")

    run_once(rt, sync_interval=timedelta(hours=1))
    spans = exporter.get_finished_spans()

    roots = [s for s in spans if s.parent is None]
    assert [s.name for s in roots] == ["sync", "due_cases", "draft_message", "deliver_message"]
    assert len({s.context.trace_id for s in roots}) == 4  # one trace per step

    [sync] = named(spans, "sync")
    assert sync.attributes is not None
    assert sync.attributes["parley.tenant_id"] == str(tenant_id)
    assert sync.attributes["parley.invoices_created"] == 1

    [due] = named(spans, "due_cases")
    assert due.attributes is not None
    assert len(due.attributes["parley.reminded_case_ids"]) == 1  # type: ignore[arg-type]

    [draft] = named(spans, "draft_message")
    model_calls = named(spans, "model_call")
    assert {s.parent.span_id for s in model_calls if s.parent} == {draft.context.span_id}
    assert sorted(s.attributes["parley.prompt_version"] for s in model_calls if s.attributes) == [
        "draft_reminder.v1",
        "tone_judge.v1",
    ]
    assert all(s.attributes and "gen_ai.usage.input_tokens" in s.attributes for s in model_calls)

    [deliver] = named(spans, "deliver_message")
    assert deliver.attributes is not None
    assert (deliver.attributes["parley.channel"], deliver.attributes["parley.status"]) == (
        "email",
        "sent",
    )


def test_an_error_marks_the_span(rt: Runtime) -> None:
    exporter = collected_spans()
    with pytest.raises(RuntimeError), step("sync"):
        raise RuntimeError("source down")
    [span] = exporter.get_finished_spans()
    assert not span.status.is_ok
    assert span.events[0].name == "exception"


def test_log_lines_carry_the_trace_id() -> None:
    collected_spans()
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", None, None)
    _TraceIdFilter().filter(record)
    assert record.trace_id == "-"  # type: ignore[attr-defined]

    with step("sync") as span:
        _TraceIdFilter().filter(record)
    assert record.trace_id == format(span.get_span_context().trace_id, "032x")  # type: ignore[attr-defined]
