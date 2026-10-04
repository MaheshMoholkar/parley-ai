"""Is a queued reminder still right to send?

A reminder is queued when its cases fall due, but it may wait hours or days
before it goes out: for drafting, for a person to approve it, or for quiet
hours to end. Things can change meanwhile. This module decides, from the
current state, whether it should still go:

- the customer or one of its cases has been paused;
- a case is no longer waiting on this reminder (paid, closed, promised,
  disputed, or handed to a person);
- the customer is on hold for a dispute or a payment claim;
- an amount, due date or invoice number in the text no longer matches the
  books (email only; a call reads the books itself).

Such a reminder is withdrawn: never sent, its approval task cancelled, and its
cases scheduled again (`on_reminder_withdrawn`), so the next reminder is drafted
from fresh facts. Messages a customer asked for (a payment link from a call)
are not reminders and are always sent.

`withdraw_stale_messages` runs every worker round before drafting, and
delivery runs `stale_reason` again just before it sends.
"""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from parley.core.checks import check_draft
from parley.core.domain import UNSENT_STATUSES, CaseState, Direction, MessageStatus, TaskStatus
from parley.core.workflow import on_reminder_withdrawn
from parley.db.models import Customer, Message, MessageCase, Task, Tenant
from parley.services.cases import (
    apply_transition,
    cases_of_message,
    customer_on_hold,
    invoice_line,
    trusted_text,
)
from parley.services.runtime import Runtime


def is_reminder(session: Session, message_id: uuid.UUID) -> bool:
    query = select(MessageCase.message_id).where(
        MessageCase.message_id == message_id, MessageCase.reminder_number.is_not(None)
    )
    return session.scalar(query.limit(1)) is not None


def stale_reason(session: Session, message: Message) -> str | None:
    """Why this unsent reminder should no longer go out, or None if it may.
    Locks the message's cases, so a sync cannot change them meanwhile."""
    if not is_reminder(session, message.id):
        return None
    customer = session.get_one(Customer, message.customer_id)
    if customer.paused:
        return "the customer was paused"
    cases = cases_of_message(session, message.id, lock=True)
    for case in cases:
        if case.paused:
            return f"invoice {case.invoice.number} was paused"
        if case.state != CaseState.AWAITING_REPLY:
            return f"invoice {case.invoice.number} is now {case.state}"
    if customer_on_hold(session, customer.id):
        return "the customer has a dispute or payment claim being looked into"
    if message.channel == "email":
        tenant = session.get_one(Tenant, message.tenant_id)
        problems = check_draft(
            message.subject,
            message.body,
            [invoice_line(c.invoice) for c in cases],
            trusted_text(tenant.name, customer.name, tenant.payment_link),
        )
        if problems:
            return "the invoices changed since it was written: " + "; ".join(problems)
    return None


def withdraw(session: Session, message: Message, reason: str, now: datetime) -> None:
    message.status = MessageStatus.WITHDRAWN
    message.last_error = f"Withdrawn: {reason}"
    open_tasks = session.scalars(
        select(Task).where(Task.message_id == message.id, Task.status == TaskStatus.OPEN)
    )
    for task in open_tasks:
        task.status = TaskStatus.CANCELLED
        task.resolution = f"The reminder was withdrawn: {reason}"
        task.resolved_at = now
    for case in cases_of_message(session, message.id, lock=True):
        transition = on_reminder_withdrawn(case.view(), now)
        if transition is not None:
            apply_transition(session, case, transition, now)


def withdraw_stale_messages(rt: Runtime, tenant_id: uuid.UUID) -> int:
    """Withdraw every unsent reminder of the tenant that should no longer go out.
    Returns how many were withdrawn."""
    withdrawn = 0
    tried: set[uuid.UUID] = set()
    while True:
        with rt.session_factory.begin() as session:
            query = (
                select(Message)
                .where(
                    Message.tenant_id == tenant_id,
                    Message.direction == Direction.OUTBOUND,
                    Message.status.in_(UNSENT_STATUSES),
                )
                .order_by(Message.created_at, Message.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if tried:
                query = query.where(Message.id.not_in(tried))
            message = session.scalar(query)
            if message is None:
                return withdrawn
            tried.add(message.id)
            reason = stale_reason(session, message)
            if reason is not None:
                withdraw(session, message, reason, rt.clock.now())
                withdrawn += 1
