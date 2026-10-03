import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from parley.adapters.clock import FakeClock
from parley.core.domain import CaseState, MessageStatus, TaskKind, TaskStatus
from parley.db.models import Case, Customer, Invoice, Message, MessageCase, Task
from parley.services.delivery import deliver_pending_messages
from parley.services.drafting import draft_queued_messages
from parley.services.due_cases import run_due_cases
from parley.services.runtime import Runtime
from parley.services.sync import sync_tenant
from tests.integration.conftest import IST, invoice_row, make_tenant, write_aging

# The test clock starts Monday 2026-01-05 10:00 IST. With the default policy an
# invoice due 2026-01-01 has its first reminder due from 2026-01-04 00:00.
ASHA_1 = invoice_row("A-1", "Asha", "1000", "2026-01-01")
ASHA_2 = invoice_row("A-2", "Asha", "250", "2025-12-28")
BALA_1 = invoice_row("B-1", "Bala", "700", "2026-01-01")


def setup(rt: Runtime, tmp_path: Path, rows: list[dict[str, str]], **policy: object) -> uuid.UUID:
    tenant_id = make_tenant(rt, write_aging(tmp_path / "aging.csv", rows), **policy)
    sync_tenant(rt, tenant_id)
    return tenant_id


def messages(rt: Runtime) -> list[Message]:
    with rt.session_factory() as session:
        return list(session.scalars(select(Message).order_by(Message.created_at)))


def case(rt: Runtime, number: str) -> Case:
    with rt.session_factory() as session:
        return session.scalars(select(Case).join(Invoice).where(Invoice.number == number)).one()


def test_due_cases_of_one_customer_go_out_as_one_message(rt: Runtime, tmp_path: Path) -> None:
    setup(rt, tmp_path, [ASHA_1, ASHA_2, BALA_1])
    tenant_id = case(rt, "A-1").tenant_id

    run_due_cases(rt, tenant_id)

    sent = messages(rt)
    assert len(sent) == 2  # one for Asha (two invoices), one for Bala
    asha = next(m for m in sent if m.to_address == "asha@example.com")
    # Queued with the template; drafting and delivery are separate steps.
    assert asha.status == MessageStatus.DRAFTING
    assert "A-1" in asha.body and "A-2" in asha.body
    for number in ("A-1", "A-2", "B-1"):
        c = case(rt, number)
        assert (c.state, c.reminders_sent) == (CaseState.AWAITING_REPLY, 1)
        assert c.next_action_at == rt.clock.now() + timedelta(days=5)


def test_nothing_happens_before_a_case_is_due(
    rt: Runtime, tmp_path: Path, clock: FakeClock
) -> None:
    clock.set(datetime(2026, 1, 3, 10, 0, tzinfo=IST))  # first reminder due from the 4th
    tenant_id = setup(rt, tmp_path, [ASHA_1])
    assert run_due_cases(rt, tenant_id) == 0
    assert messages(rt) == []


def test_quiet_hours_delay_the_reminder(rt: Runtime, tmp_path: Path, clock: FakeClock) -> None:
    clock.set(datetime(2026, 1, 5, 21, 0, tzinfo=IST))
    tenant_id = setup(rt, tmp_path, [ASHA_1])

    run_due_cases(rt, tenant_id)

    assert messages(rt) == []
    c = case(rt, "A-1")
    assert c.state == CaseState.SCHEDULED
    assert c.next_action_at == datetime(2026, 1, 6, 9, 0, tzinfo=IST)


def run_round(rt: Runtime, tenant_id: uuid.UUID) -> None:
    """Act on due cases, then draft and deliver what was queued."""
    run_due_cases(rt, tenant_id)
    draft_queued_messages(rt, tenant_id)
    deliver_pending_messages(rt, tenant_id)


def test_weekly_cap_delays_a_third_message(rt: Runtime, tmp_path: Path, clock: FakeClock) -> None:
    tenant_id = setup(rt, tmp_path, [ASHA_1], reminder_gap_days=1, approval_mode="none")
    run_round(rt, tenant_id)  # Monday: message 1
    clock.advance(timedelta(days=2))
    run_round(rt, tenant_id)  # Wednesday: back to Scheduled, message 2
    clock.advance(timedelta(days=2))
    run_round(rt, tenant_id)  # Friday: the cap of 2 per week is reached

    assert len(messages(rt)) == 2
    c = case(rt, "A-1")
    assert c.state == CaseState.SCHEDULED
    # Monday's message leaves the 7-day window next Monday at 10:00.
    assert c.next_action_at == datetime(2026, 1, 12, 10, 0, tzinfo=IST)


def test_next_reminder_waits_while_the_last_one_awaits_approval(
    rt: Runtime, tmp_path: Path, clock: FakeClock
) -> None:
    tenant_id = setup(rt, tmp_path, [ASHA_1], reminder_gap_days=1)  # approval_mode "all"
    run_round(rt, tenant_id)
    assert [m.status for m in messages(rt)] == [MessageStatus.AWAITING_APPROVAL]

    clock.advance(timedelta(days=2))
    run_round(rt, tenant_id)

    assert len(messages(rt)) == 1  # no second reminder while the first is unsent
    c = case(rt, "A-1")
    assert (c.state, c.next_action_at) == (CaseState.SCHEDULED, rt.clock.now() + timedelta(days=1))


def test_customer_without_email_goes_to_a_human(rt: Runtime, tmp_path: Path) -> None:
    tenant_id = setup(rt, tmp_path, [{**ASHA_1, "email": ""}])
    with rt.session_factory.begin() as session:
        session.scalars(select(Customer)).one().email = None

    run_due_cases(rt, tenant_id)

    assert messages(rt) == []
    assert case(rt, "A-1").state == CaseState.NEEDS_HUMAN
    with rt.session_factory() as session:
        task = session.scalars(select(Task)).one()
        assert task.kind == TaskKind.ESCALATION
        assert task.summary == "Customer has no email address or phone number we may call."


def test_reminder_limit_escalates(rt: Runtime, tmp_path: Path) -> None:
    tenant_id = setup(rt, tmp_path, [ASHA_1], max_reminders=1)
    with rt.session_factory.begin() as session:
        session.scalars(select(Case)).one().reminders_sent = 1

    run_due_cases(rt, tenant_id)

    assert messages(rt) == []
    assert case(rt, "A-1").state == CaseState.NEEDS_HUMAN


def test_paused_customer_and_paused_case_receive_nothing(rt: Runtime, tmp_path: Path) -> None:
    tenant_id = setup(rt, tmp_path, [ASHA_1, BALA_1])
    with rt.session_factory.begin() as session:
        session.scalars(select(Customer).where(Customer.name == "Asha")).one().paused = True
        session.scalars(
            select(Case).join(Invoice).where(Invoice.number == "B-1")
        ).one().paused = True

    run_due_cases(rt, tenant_id)
    assert messages(rt) == []

    with rt.session_factory.begin() as session:
        session.scalars(select(Customer).where(Customer.name == "Asha")).one().paused = False
    run_due_cases(rt, tenant_id)
    assert [m.to_address for m in messages(rt)] == ["asha@example.com"]


def test_open_dispute_holds_every_reminder_to_that_customer(rt: Runtime, tmp_path: Path) -> None:
    tenant_id = setup(rt, tmp_path, [ASHA_1, ASHA_2])
    with rt.session_factory.begin() as session:
        disputed = session.scalars(select(Case).join(Invoice).where(Invoice.number == "A-2")).one()
        disputed.state = CaseState.NEEDS_HUMAN
        disputed.next_action_at = None
        session.add(
            Task(
                tenant_id=tenant_id,
                case_id=disputed.id,
                kind=TaskKind.REVIEW_DISPUTE,
                summary="Says goods were damaged",
                status=TaskStatus.OPEN,
                created_at=rt.clock.now(),
            )
        )

    run_due_cases(rt, tenant_id)

    assert messages(rt) == []
    c = case(rt, "A-1")
    assert c.state == CaseState.SCHEDULED
    assert c.next_action_at == rt.clock.now() + timedelta(days=1)


def test_a_customer_locked_by_another_worker_is_skipped(rt: Runtime, tmp_path: Path) -> None:
    tenant_id = setup(rt, tmp_path, [ASHA_1, BALA_1])
    with rt.session_factory.begin() as other_worker:
        # Simulate a second worker holding Asha's row lock mid-transaction.
        other_worker.scalars(
            select(Customer).where(Customer.name == "Asha").with_for_update()
        ).one()
        run_due_cases(rt, tenant_id)

    assert [m.to_address for m in messages(rt)] == ["bala@example.com"]


def test_one_failing_customer_does_not_block_the_others(
    rt: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id = setup(rt, tmp_path, [ASHA_1, BALA_1])
    from parley.services import due_cases

    original = due_cases._queue_reminder

    def fail_for_asha(session, tenant, customer, cases, now, call_number):  # type: ignore[no-untyped-def]
        if customer.name == "Asha":
            raise RuntimeError("boom")
        original(session, tenant, customer, cases, now, call_number)

    monkeypatch.setattr(due_cases, "_queue_reminder", fail_for_asha)
    run_due_cases(rt, tenant_id)

    assert [m.to_address for m in messages(rt)] == ["bala@example.com"]
    # Asha's transaction was rolled back, so her case is untouched and retried next run.
    a1 = case(rt, "A-1")
    assert (a1.state, a1.reminders_sent) == (CaseState.SCHEDULED, 0)


def test_a_reminder_number_can_only_be_recorded_once(rt: Runtime, tmp_path: Path) -> None:
    tenant_id = setup(rt, tmp_path, [ASHA_1])
    run_due_cases(rt, tenant_id)
    link = None
    with rt.session_factory() as session:
        link = session.scalars(select(MessageCase)).one()
        message = session.get_one(Message, link.message_id)

    with pytest.raises(IntegrityError), rt.session_factory.begin() as session:
        copy = Message(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            customer_id=message.customer_id,
            channel="email",
            direction=message.direction,
            to_address=message.to_address,
            subject="dup",
            body="dup",
            status=MessageStatus.PENDING,
            idempotency_key=uuid.uuid4().hex,
            attempts=0,
            created_at=rt.clock.now(),
        )
        session.add(copy)
        session.flush()
        session.add(
            MessageCase(
                message_id=copy.id,
                case_id=link.case_id,
                tenant_id=tenant_id,
                reminder_number=link.reminder_number,
            )
        )
