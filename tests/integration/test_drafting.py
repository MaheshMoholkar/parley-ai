"""Drafting with a (fake) model: checks, regeneration, tone judge, approval, logging."""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from parley.adapters.models.fake import FakeCall, FakeModel
from parley.collections_ai.jobs import DraftOut, ToneVerdict
from parley.core.domain import MessageStatus, TaskKind
from parley.db.models import Message, ModelCall, Task
from parley.ports.model import ModelError
from parley.services.drafting import draft_queued_messages
from parley.services.due_cases import run_due_cases
from parley.services.runtime import Runtime
from parley.services.sync import sync_tenant
from tests.integration.conftest import invoice_row, make_tenant, write_aging

ROW = invoice_row("A-1", "Asha", "1234.50", "2026-01-01")
GOOD = DraftOut(
    subject="Invoice A-1 is overdue",
    body="Dear Asha,\nInvoice A-1 for INR 1,234.50 was due on 01 Jan 2026.\nRegards, Acme",
)
WRONG_AMOUNT = DraftOut(subject=GOOD.subject, body=GOOD.body.replace("1,234.50", "1,000.00"))


def model_answering(drafts: list[DraftOut], tone_ok: bool = True) -> FakeModel:
    """A fake model that returns `drafts` in order, and a fixed tone verdict."""
    queue = list(drafts)

    def respond(call: FakeCall) -> DraftOut | ToneVerdict:
        if call.output_type is DraftOut:
            return queue.pop(0)
        return ToneVerdict(passed=tone_ok, reason="ok" if tone_ok else "sounds threatening")

    return FakeModel(respond)


def queue_reminder(rt: Runtime, tmp_path: Path, **policy: object) -> uuid.UUID:
    tenant_id = make_tenant(rt, write_aging(tmp_path / "aging.csv", [ROW]), **policy)
    sync_tenant(rt, tenant_id)
    run_due_cases(rt, tenant_id)
    return tenant_id


def the_message(rt: Runtime) -> Message:
    with rt.session_factory() as session:
        return session.scalars(select(Message)).one()


def tasks(rt: Runtime) -> list[Task]:
    with rt.session_factory() as session:
        return list(session.scalars(select(Task)))


def test_good_draft_goes_to_the_outbox(rt: Runtime, tmp_path: Path) -> None:
    rt.model = model = model_answering([GOOD])
    rt.model_prices = {"fake-large": (4.0, 20.0), "fake-small": (2.0, 10.0)}
    tenant_id = queue_reminder(rt, tmp_path, approval_mode="none")

    assert draft_queued_messages(rt, tenant_id) == 1

    message = the_message(rt)
    assert message.status == MessageStatus.PENDING
    assert (message.subject, message.body) == (GOOD.subject, GOOD.body)
    assert message.prompt_version == "draft_reminder.v1"
    # The facts sent to the model use the exact display strings.
    assert '"amount_due": "INR 1,234.50"' in model.calls[0].prompt
    assert '"due_date": "01 Jan 2026"' in model.calls[0].prompt

    with rt.session_factory() as session:
        calls = session.scalars(select(ModelCall).order_by(ModelCall.prompt_version)).all()
    assert [(c.prompt_version, c.tier, c.ok) for c in calls] == [
        ("draft_reminder.v1", "large", True),
        ("tone_judge.v1", "small", True),
    ]
    # 100 input tokens at $4/M plus 50 output tokens at $20/M = 1400 micro-dollars.
    assert calls[0].cost_micro_usd == 1400


def test_approval_mode_all_waits_for_a_person(rt: Runtime, tmp_path: Path) -> None:
    rt.model = model_answering([GOOD])
    tenant_id = queue_reminder(rt, tmp_path)  # default approval_mode "all"

    draft_queued_messages(rt, tenant_id)

    message = the_message(rt)
    assert message.status == MessageStatus.AWAITING_APPROVAL
    [task] = tasks(rt)
    assert (task.kind, task.message_id) == (TaskKind.APPROVE_SEND, message.id)
    assert "A-1 (INR 1,234.50)" in task.summary


def test_failed_draft_is_regenerated_once(rt: Runtime, tmp_path: Path) -> None:
    rt.model = model = model_answering([WRONG_AMOUNT, GOOD])
    tenant_id = queue_reminder(rt, tmp_path, approval_mode="none")

    draft_queued_messages(rt, tenant_id)

    assert the_message(rt).body == GOOD.body
    assert [c.output_type for c in model.calls] == [DraftOut, DraftOut, ToneVerdict]


def test_two_failed_drafts_fall_back_to_the_template_for_approval(
    rt: Runtime, tmp_path: Path
) -> None:
    rt.model = model_answering([WRONG_AMOUNT, WRONG_AMOUNT])
    tenant_id = queue_reminder(rt, tmp_path, approval_mode="none")

    draft_queued_messages(rt, tenant_id)

    message = the_message(rt)
    assert message.status == MessageStatus.AWAITING_APPROVAL  # even with approval off
    assert "friendly reminder" in message.body  # the fixed template, never the bad draft
    assert message.prompt_version is None
    [task] = tasks(rt)
    assert task.kind == TaskKind.APPROVE_SEND
    assert "does not match any amount due" in task.summary


def test_tone_judge_can_block_a_draft(rt: Runtime, tmp_path: Path) -> None:
    rt.model = model_answering([GOOD, GOOD], tone_ok=False)
    tenant_id = queue_reminder(rt, tmp_path, approval_mode="none")

    draft_queued_messages(rt, tenant_id)

    assert the_message(rt).status == MessageStatus.AWAITING_APPROVAL
    assert "tone judge: sounds threatening" in tasks(rt)[0].summary


def test_model_errors_are_logged_and_fall_back(rt: Runtime, tmp_path: Path) -> None:
    def broken(call: FakeCall) -> DraftOut:
        raise ModelError("ThrottlingException")

    rt.model = FakeModel(broken)
    tenant_id = queue_reminder(rt, tmp_path, approval_mode="none")

    draft_queued_messages(rt, tenant_id)

    assert the_message(rt).status == MessageStatus.AWAITING_APPROVAL
    with rt.session_factory() as session:
        calls = session.scalars(select(ModelCall)).all()
    assert [(c.ok, c.error) for c in calls] == [(False, "ThrottlingException")] * 2


@pytest.mark.parametrize(
    ("mode", "status"),
    [("none", MessageStatus.PENDING), ("all", MessageStatus.AWAITING_APPROVAL)],
)
def test_without_a_model_the_template_is_used(
    rt: Runtime, tmp_path: Path, mode: str, status: MessageStatus
) -> None:
    tenant_id = queue_reminder(rt, tmp_path, approval_mode=mode)
    draft_queued_messages(rt, tenant_id)
    message = the_message(rt)
    assert message.status == status
    assert "friendly reminder" in message.body
