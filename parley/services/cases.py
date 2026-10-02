"""Opening cases, applying workflow transitions, and pausing."""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from parley.core.domain import (
    AgentRunStatus,
    CaseState,
    DisputeStatus,
    InvoiceStatus,
    PromiseStatus,
    TaskStatus,
)
from parley.core.messages import InvoiceLine
from parley.core.workflow import Transition, first_reminder_at
from parley.db.models import (
    AgentRun,
    Case,
    Customer,
    Dispute,
    Invoice,
    MessageCase,
    Promise,
    Task,
    Tenant,
)


class NotFoundError(LookupError):
    pass


def apply_transition(
    session: Session,
    case: Case,
    transition: Transition,
    now: datetime,
    source_message_id: uuid.UUID | None = None,
    agent_run_id: uuid.UUID | None = None,
) -> None:
    """Save a workflow decision onto a case. Sending a reminder is left to the caller,
    because one message can cover several cases. `source_message_id` is the reply
    that caused the change, recorded on any promise it creates; `agent_run_id` is
    the investigation behind any task it creates."""
    reopening = case.state == CaseState.CLOSED and transition.state != CaseState.CLOSED

    case.state = transition.state
    case.next_action_at = transition.next_action_at
    case.extra_reminders += transition.grant_extra_reminders

    if transition.state == CaseState.CLOSED:
        case.closed_at = now
        case.closed_reason = transition.close_reason
        _cancel_open_tasks(session, case, now)
    elif reopening:
        case.closed_at = None
        case.closed_reason = None

    if transition.task is not None:
        session.add(
            Task(
                tenant_id=case.tenant_id,
                case_id=case.id,
                kind=transition.task,
                summary=transition.task_summary,
                status=TaskStatus.OPEN,
                agent_run_id=agent_run_id,
                created_at=now,
            )
        )

    if transition.promise_outcome is not None:
        open_promises = session.scalars(
            select(Promise).where(Promise.case_id == case.id, Promise.status == PromiseStatus.OPEN)
        )
        for promise in open_promises:
            promise.status = transition.promise_outcome

    if transition.new_dispute is not None:
        session.add(
            Dispute(
                tenant_id=case.tenant_id,
                case_id=case.id,
                reason=transition.new_dispute,
                status=DisputeStatus.OPEN,
                created_at=now,
            )
        )

    if transition.investigate is not None:
        session.add(
            AgentRun(
                tenant_id=case.tenant_id,
                case_id=case.id,
                message_id=source_message_id,
                claim=transition.investigate,
                status=AgentRunStatus.QUEUED,
                steps=[],
                created_at=now,
            )
        )

    if transition.new_promise is not None:
        session.add(
            Promise(
                tenant_id=case.tenant_id,
                case_id=case.id,
                amount=transition.new_promise.amount,
                promised_date=transition.new_promise.promised_date,
                status=PromiseStatus.OPEN,
                source_message_id=source_message_id,
            )
        )


def _cancel_open_tasks(session: Session, case: Case, now: datetime) -> None:
    open_tasks = session.scalars(
        select(Task).where(Task.case_id == case.id, Task.status == TaskStatus.OPEN)
    )
    for task in open_tasks:
        task.status = TaskStatus.CANCELLED
        task.resolution = f"Case closed: {case.closed_reason}"
        task.resolved_at = now


def open_overdue_cases(session: Session, tenant: Tenant, now: datetime) -> int:
    """Open a Scheduled case for every open invoice past its due date that has none.

    Safe to run from several workers at once: the unique index on `invoice_id`
    makes a second insert for the same invoice do nothing.
    """
    today = now.astimezone(tenant.zone).date()
    has_case = select(Case.id).where(Case.invoice_id == Invoice.id).exists()
    overdue = session.scalars(
        select(Invoice).where(
            Invoice.tenant_id == tenant.id,
            Invoice.status == InvoiceStatus.OPEN,
            Invoice.amount_due > 0,
            Invoice.due_date < today,
            ~has_case,
        )
    ).all()

    opened = 0
    policy = tenant.policy
    for invoice in overdue:
        statement = (
            insert(Case)
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                invoice_id=invoice.id,
                customer_id=invoice.customer_id,
                state=CaseState.SCHEDULED,
                paused=False,
                reminders_sent=0,
                next_action_at=first_reminder_at(invoice.due_date, policy, tenant.zone),
                opened_at=now,
            )
            .on_conflict_do_nothing(index_elements=[Case.invoice_id])
            .returning(Case.id)
        )
        if session.execute(statement).scalar_one_or_none() is not None:
            opened += 1
    return opened


# --- Pause and resume ------------------------------------------------------------
# Pause is a flag, not a state: the case keeps its state and timer, and the
# worker skips it until it is resumed.


def set_case_paused(
    session: Session, tenant_id: uuid.UUID, case_id: uuid.UUID, paused: bool
) -> Case:
    case = session.scalar(
        select(Case).where(Case.id == case_id, Case.tenant_id == tenant_id).with_for_update()
    )
    if case is None:
        raise NotFoundError(f"case {case_id} not found")
    case.paused = paused
    return case


def set_customer_paused(
    session: Session, tenant_id: uuid.UUID, customer_id: uuid.UUID, paused: bool
) -> Customer:
    customer = session.scalar(
        select(Customer)
        .where(Customer.id == customer_id, Customer.tenant_id == tenant_id)
        .with_for_update()
    )
    if customer is None:
        raise NotFoundError(f"customer {customer_id} not found")
    customer.paused = paused
    return customer


# --- Helpers shared by the message steps ---------------------------------------------


def cases_of_message(session: Session, message_id: uuid.UUID, lock: bool = False) -> list[Case]:
    query = (
        select(Case)
        .join(MessageCase, MessageCase.case_id == Case.id)
        .where(MessageCase.message_id == message_id)
        .order_by(Case.opened_at, Case.id)
    )
    if lock:
        query = query.with_for_update(of=Case)
    return list(session.scalars(query))


def invoice_line(invoice: Invoice) -> InvoiceLine:
    return InvoiceLine(
        number=invoice.number,
        amount_due=invoice.amount_due,
        currency=invoice.currency,
        due_date=invoice.due_date,
        display_details=invoice.display_details,
    )
