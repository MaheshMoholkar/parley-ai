"""A queued reminder is checked again before it goes out (services/sendable.py).

Reminders can wait days for approval or quiet hours. These tests change things
in that wait and check the reminder is withdrawn, not sent, and that the case
carries on.
"""

from datetime import datetime, time, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from parley.adapters.channels.dry_run import DryRunChannel
from parley.adapters.clock import FakeClock
from parley.core.domain import MessageStatus, TaskAction, TaskKind, TaskStatus
from parley.db.models import Customer, Task, Tenant
from parley.services.runtime import Runtime
from parley.services.sync import sync_tenant
from parley.services.tasks import TaskError, resolve_task
from parley.services.worker import run_once
from tests.integration.conftest import IST, START, make_tenant, write_aging
from tests.integration.test_due_cases import ASHA_1, ASHA_2, case, messages

HOUR = timedelta(hours=1)


def at(clock: FakeClock, day: int, hour: int = 10, minute: int = 0) -> None:
    clock.set(datetime.combine(START.date() + timedelta(days=day), time(hour, minute), tzinfo=IST))


def approval_task(rt: Runtime) -> Task:
    with rt.session_factory() as session:
        return session.scalars(select(Task).where(Task.kind == TaskKind.APPROVE_SEND)).one()


def approve(rt: Runtime, task: Task) -> None:
    with rt.session_factory.begin() as session:
        tenant = session.scalars(select(Tenant)).one()
        resolve_task(session, tenant, task.id, TaskAction.APPROVE, rt.clock.now())


def test_a_paid_invoice_withdraws_the_waiting_reminder_and_the_other_is_chased(
    rt: Runtime, clock: FakeClock, tmp_path: Path
) -> None:
    aging = write_aging(tmp_path / "aging.csv", [ASHA_1, ASHA_2])
    make_tenant(rt, aging)  # approval_mode "all"
    run_once(rt, HOUR)
    [first] = messages(rt)
    assert first.status == MessageStatus.AWAITING_APPROVAL

    # A-1 is paid while the reminder about both waits for approval.
    write_aging(aging, [{**ASHA_1, "amount_due": "0"}, ASHA_2])
    clock.advance(2 * HOUR)
    run_once(rt, HOUR)  # sync closes A-1; the waiting reminder is withdrawn
    run_once(rt, HOUR)  # A-2 is due again and gets its own reminder

    first, second = messages(rt)
    assert first.status == MessageStatus.WITHDRAWN
    assert "A-1 is now closed" in (first.last_error or "")
    with rt.session_factory() as session:
        statuses = {t.message_id: t.status for t in session.scalars(select(Task))}
    assert statuses[first.id] == TaskStatus.CANCELLED
    # A-2 got a fresh reminder about itself alone, and the withdrawn one does
    # not count towards its limit.
    assert second.status == MessageStatus.AWAITING_APPROVAL
    assert "A-2" in second.body and "A-1" not in second.body
    a2 = case(rt, "A-2")
    assert (a2.reminders_sent, a2.extra_reminders) == (2, 1)


def test_a_customer_paused_after_approval_gets_nothing(
    rt: Runtime, channel: DryRunChannel, clock: FakeClock, tmp_path: Path
) -> None:
    make_tenant(rt, write_aging(tmp_path / "aging.csv", [ASHA_1]))
    run_once(rt, HOUR)
    task = approval_task(rt)
    at(clock, 0, hour=19)  # before quiet hours: approval would send at once...
    approve(rt, task)
    with rt.session_factory.begin() as session:
        session.scalars(select(Customer)).one().paused = True  # ...but they pause first

    run_once(rt, HOUR)
    assert not channel.sent
    assert messages(rt)[0].status == MessageStatus.WITHDRAWN


def test_approving_a_stale_reminder_is_refused(rt: Runtime, tmp_path: Path) -> None:
    make_tenant(rt, write_aging(tmp_path / "aging.csv", [ASHA_1]))
    run_once(rt, HOUR)
    task = approval_task(rt)
    with rt.session_factory.begin() as session:
        session.scalars(select(Customer)).one().paused = True
    with pytest.raises(TaskError, match="the customer was paused"):
        approve(rt, task)


def test_a_reminder_approved_at_night_waits_for_the_morning(
    rt: Runtime, channel: DryRunChannel, clock: FakeClock, tmp_path: Path
) -> None:
    make_tenant(rt, write_aging(tmp_path / "aging.csv", [ASHA_1]))
    run_once(rt, HOUR)
    at(clock, 0, hour=22, minute=30)
    approve(rt, approval_task(rt))

    run_once(rt, HOUR)
    assert not channel.sent
    at(clock, 1, hour=9)
    run_once(rt, HOUR)
    assert len(channel.sent) == 1


def test_the_wait_for_a_reply_starts_when_the_reminder_is_sent(
    rt: Runtime, channel: DryRunChannel, clock: FakeClock, tmp_path: Path
) -> None:
    make_tenant(rt, write_aging(tmp_path / "aging.csv", [ASHA_1]))  # gap 5 days
    run_once(rt, HOUR)
    at(clock, 4)  # approved and sent four days after it was queued
    approve(rt, approval_task(rt))
    run_once(rt, HOUR)
    assert len(channel.sent) == 1
    assert case(rt, "A-1").next_action_at == clock.now() + timedelta(days=5)

    at(clock, 8)  # five days after queueing, but only four after sending
    run_once(rt, HOUR)
    assert len(messages(rt)) == 1


def test_a_changed_amount_withdraws_the_reminder_and_a_fresh_one_is_written(
    rt: Runtime, channel: DryRunChannel, clock: FakeClock, tmp_path: Path
) -> None:
    aging = write_aging(tmp_path / "aging.csv", [ASHA_1])
    make_tenant(rt, aging)
    run_once(rt, HOUR)
    approve(rt, approval_task(rt))  # approved with INR 1,000.00 in the text
    # A part payment is recorded before delivery (a sync in another worker).
    write_aging(aging, [{**ASHA_1, "amount_due": "600"}])
    sync_tenant(rt, case(rt, "A-1").tenant_id)
    run_once(rt, HOUR)  # withdrawn at delivery
    run_once(rt, HOUR)  # a fresh one is queued with the new amount

    assert not channel.sent
    old, new = messages(rt)
    assert old.status == MessageStatus.WITHDRAWN
    assert "INR 600.00" in new.body
